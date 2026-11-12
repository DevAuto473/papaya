"""SQLite storage: accounts, per-account settings, seen items, sent-notification log."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .parser import Item, RIYADH

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  name TEXT PRIMARY KEY,
  chat_id INTEGER,
  baselined INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(
  account TEXT PRIMARY KEY,
  remind_hours REAL NOT NULL DEFAULT 10,
  quiet_start INTEGER NOT NULL DEFAULT 23,
  quiet_end INTEGER NOT NULL DEFAULT 6,
  notify_new INTEGER NOT NULL DEFAULT 1,
  notify_remind INTEGER NOT NULL DEFAULT 1,
  notify_opened INTEGER NOT NULL DEFAULT 1,
  remind_external INTEGER NOT NULL DEFAULT 1,
  horizon_days INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS items(
  account TEXT NOT NULL,
  kind TEXT NOT NULL,
  id TEXT NOT NULL,
  title TEXT, subject TEXT, status TEXT, solve_url TEXT,
  deadline TEXT NOT NULL, opens_at TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  PRIMARY KEY(account, kind, id)
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS logs(ts TEXT NOT NULL, account TEXT, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS solutions(
  account TEXT NOT NULL, item_id TEXT NOT NULL,
  data TEXT NOT NULL,            -- JSON: questions + answers
  status TEXT NOT NULL,          -- ready | submitted | dropped | failed
  all_agree INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(account, item_id)
);
CREATE TABLE IF NOT EXISTS done_marks(
  account TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL, ts TEXT NOT NULL,
  PRIMARY KEY(account, kind, item_id)
);
CREATE TABLE IF NOT EXISTS msgs(
  chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, ts TEXT NOT NULL,
  PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS sent(
  account TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL, event TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY(account, kind, item_id, event)
);
"""


@dataclass
class Settings:
    remind_hours: float = 10
    quiet_start: int = 23
    quiet_end: int = 6
    notify_new: bool = True
    notify_remind: bool = True
    notify_opened: bool = True
    remind_external: bool = True      # also used as "show externally-submitted items"
    horizon_days: int = 1             # 1 = next 24 hours, N = within N calendar days, 0 = all
    auto_solve: bool = True           # solver: prepare a solution 5h before the deadline
    auto_submit: bool = False         # solver: submit by itself 1h before, only if every answer agreed
    direct_submit: bool = False       # solver: submit right after solving, no preview tap (owner's choice)
    autoclean: bool = True            # delete chat messages older than 24 hours


class DB:
    def __init__(self, path: str | Path = "papaya.db"):
        self.con = sqlite3.connect(str(path))
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        # upgrade older databases in place
        self._add_column("settings", "horizon_days", "INTEGER NOT NULL DEFAULT 2")
        self._add_column("settings", "auto_solve", "INTEGER NOT NULL DEFAULT 1")
        self._add_column("settings", "auto_submit", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("accounts", "solver", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("settings", "direct_submit", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("settings", "autoclean", "INTEGER NOT NULL DEFAULT 1")
        if not self.get_meta("horizon_24h"):          # one time: the old default (today+tomorrow) -> 24 hours
            self.con.execute("UPDATE settings SET horizon_days=1 WHERE horizon_days=2")
            self.set_meta("horizon_24h", "1")

    def _add_column(self, table: str, col: str, decl: str):
        cols = {r["name"] for r in self.con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            self.con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.con.commit()

    # accounts ---------------------------------------------------------
    def ensure_account(self, name: str):
        self.con.execute("INSERT OR IGNORE INTO accounts(name) VALUES(?)", (name,))
        self.con.execute("INSERT OR IGNORE INTO settings(account) VALUES(?)", (name,))
        self.con.commit()

    def set_chat(self, name: str, chat_id: int):
        self.ensure_account(name)
        # one phone = one account: a chat linked again moves, it is never shared by two accounts
        self.con.execute("UPDATE accounts SET chat_id=NULL WHERE chat_id=? AND name<>?", (chat_id, name))
        self.con.execute("UPDATE accounts SET chat_id=? WHERE name=?", (chat_id, name))
        self.con.commit()

    def chat_id(self, name: str):
        r = self.con.execute("SELECT chat_id FROM accounts WHERE name=?", (name,)).fetchone()
        return r["chat_id"] if r else None

    def is_baselined(self, name: str) -> bool:
        r = self.con.execute("SELECT baselined FROM accounts WHERE name=?", (name,)).fetchone()
        return bool(r and r["baselined"])

    def mark_baselined(self, name: str):
        self.con.execute("UPDATE accounts SET baselined=1 WHERE name=?", (name,))
        self.con.commit()

    # settings ---------------------------------------------------------
    def get_settings(self, name: str) -> Settings:
        self.ensure_account(name)
        r = self.con.execute("SELECT * FROM settings WHERE account=?", (name,)).fetchone()
        return Settings(
            remind_hours=r["remind_hours"], quiet_start=r["quiet_start"], quiet_end=r["quiet_end"],
            notify_new=bool(r["notify_new"]), notify_remind=bool(r["notify_remind"]),
            notify_opened=bool(r["notify_opened"]), remind_external=bool(r["remind_external"]),
            horizon_days=int(r["horizon_days"]),
            auto_solve=bool(r["auto_solve"]), auto_submit=bool(r["auto_submit"]),
            direct_submit=bool(r["direct_submit"]), autoclean=bool(r["autoclean"]),
        )

    def set_setting(self, name: str, key: str, value):
        if key not in asdict(Settings()):
            raise KeyError(key)
        self.ensure_account(name)
        self.con.execute(f"UPDATE settings SET {key}=? WHERE account=?", (value, name))
        self.con.commit()

    # items ------------------------------------------------------------
    def known(self, name: str) -> dict[tuple[str, str], sqlite3.Row]:
        rows = self.con.execute("SELECT * FROM items WHERE account=?", (name,)).fetchall()
        return {(r["kind"], r["id"]): r for r in rows}

    def sent_events(self, name: str) -> set[tuple[str, str, str]]:
        rows = self.con.execute("SELECT kind,item_id,event FROM sent WHERE account=?", (name,)).fetchall()
        return {(r["kind"], r["item_id"], r["event"]) for r in rows}

    def upsert_items(self, name: str, items: list[Item], now: datetime):
        ts = now.isoformat()
        for it in items:
            self.con.execute(
                """INSERT INTO items(account,kind,id,title,subject,status,solve_url,deadline,opens_at,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account,kind,id) DO UPDATE SET
                     title=excluded.title, subject=excluded.subject, status=excluded.status,
                     solve_url=excluded.solve_url, deadline=excluded.deadline,
                     opens_at=excluded.opens_at, last_seen=excluded.last_seen""",
                (name, it.kind, it.id, it.title, it.subject, it.status, it.solve_url,
                 it.deadline.isoformat(), it.opens_at.isoformat() if it.opens_at else None, ts, ts),
            )
        self.con.commit()

    def record_sent(self, name: str, kind: str, item_id: str, event: str, now: datetime):
        self.con.execute("INSERT OR IGNORE INTO sent VALUES(?,?,?,?,?)",
                         (name, kind, item_id, event, now.isoformat()))
        self.con.commit()

    # lookups used by the bot -------------------------------------------
    def accounts(self) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM accounts ORDER BY name").fetchall()

    def account_for_chat(self, chat_id: int):
        r = self.con.execute("SELECT name FROM accounts WHERE chat_id=?", (chat_id,)).fetchone()
        return r["name"] if r else None

    def unlink(self, name: str):
        self.con.execute("UPDATE accounts SET chat_id=NULL, baselined=0 WHERE name=?", (name,))
        self.con.commit()

    def upcoming(self, name: str, now: datetime, limit: int = 15) -> list[sqlite3.Row]:
        rows = self.con.execute("SELECT * FROM items WHERE account=?", (name,)).fetchall()
        live = [r for r in rows if datetime.fromisoformat(r["deadline"]) > now]
        live.sort(key=lambda r: r["deadline"])
        return live[:limit]

    def set_meta(self, k: str, v: str):
        self.con.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.con.commit()

    def get_meta(self, k: str, default=None):
        r = self.con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    # solver permission (admin-controlled) -------------------------------
    def solver_enabled(self, name: str) -> bool:
        r = self.con.execute("SELECT solver FROM accounts WHERE name=?", (name,)).fetchone()
        return bool(r and r["solver"])

    def set_solver(self, name: str, on: bool):
        self.ensure_account(name)
        was = self.solver_enabled(name)
        self.con.execute("UPDATE accounts SET solver=? WHERE name=?", (1 if on else 0, name))
        self.con.commit()
        if on and not was:                 # a new VIP starts with auto-solve ON (they can switch it off in settings)
            self.set_setting(name, "auto_solve", 1)

    def item(self, name: str, kind: str, item_id: str):
        return self.con.execute("SELECT * FROM items WHERE account=? AND kind=? AND id=?",
                                (name, kind, item_id)).fetchone()

    # solutions ------------------------------------------------------------
    def save_solution(self, name: str, item_id: str, data: str, all_agree: bool, now: datetime, status="ready"):
        ts = now.isoformat()
        self.con.execute(
            """INSERT INTO solutions VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(account,item_id) DO UPDATE SET data=excluded.data, status=excluded.status,
                 all_agree=excluded.all_agree, updated_at=excluded.updated_at""",
            (name, item_id, data, status, 1 if all_agree else 0, ts, ts))
        self.con.commit()

    def solution(self, name: str, item_id: str):
        return self.con.execute("SELECT * FROM solutions WHERE account=? AND item_id=?", (name, item_id)).fetchone()

    def set_solution_status(self, name: str, item_id: str, status: str, now: datetime):
        self.con.execute("UPDATE solutions SET status=?, updated_at=? WHERE account=? AND item_id=?",
                         (status, now.isoformat(), name, item_id))
        self.con.commit()

    # monthly AI spend -------------------------------------------------------
    # admin log -----------------------------------------------------------
    def log(self, account, text: str, now: datetime | None = None):
        from .parser import RIYADH
        ts = (now or datetime.now(RIYADH)).isoformat(timespec="seconds")
        self.con.execute("INSERT INTO logs VALUES(?,?,?)", (ts, account, text[:300]))
        self.con.execute("DELETE FROM logs WHERE rowid NOT IN (SELECT rowid FROM logs ORDER BY rowid DESC LIMIT 1000)")
        self.con.commit()

    def logs(self, account=None, limit: int = 25) -> list:
        q, a = "SELECT * FROM logs", ()
        if account:
            q, a = q + " WHERE account=?", (account,)
        return self.con.execute(q + " ORDER BY rowid DESC LIMIT ?", a + (limit,)).fetchall()

    def ai_stats(self, name: str, month: str) -> dict:
        return json.loads(self.get_meta(f"aistats:{name}:{month}") or '{"solved": 0, "tokens": 0, "cost": 0}')

    def add_ai_stats(self, name: str, month: str, tokens: int, cost: float):
        st = self.ai_stats(name, month)
        st = {"solved": st["solved"] + 1, "tokens": st["tokens"] + tokens, "cost": round(st["cost"] + cost, 6)}
        self.set_meta(f"aistats:{name}:{month}", json.dumps(st))
        return st

    def spend(self, month: str) -> float:
        return float(self.get_meta(f"spend:{month}", "0") or 0)

    def add_spend(self, month: str, usd: float):
        self.set_meta(f"spend:{month}", f"{self.spend(month) + usd:.6f}")

    # chat message log (for /clear) ------------------------------------------
    def log_msg(self, chat_id: int, message_id: int, now: datetime):
        self.con.execute("INSERT OR IGNORE INTO msgs VALUES(?,?,?)", (chat_id, message_id, now.isoformat()))
        self.con.commit()

    def chat_msgs(self, chat_id: int) -> list[int]:
        rows = self.con.execute("SELECT message_id FROM msgs WHERE chat_id=? ORDER BY message_id", (chat_id,))
        return [r["message_id"] for r in rows]

    def forget_msgs(self, chat_id: int, ids: list[int] | None = None):
        if ids is None:
            self.con.execute("DELETE FROM msgs WHERE chat_id=?", (chat_id,))
        else:
            self.con.executemany("DELETE FROM msgs WHERE chat_id=? AND message_id=?", [(chat_id, i) for i in ids])
        self.con.commit()

    # items the platform no longer lists / the student marked as done ----------
    def prune_items(self, name: str, keep: set):
        """Forget items that are no longer on the platform (submitted, deleted, or past)."""
        gone = [(k, i) for (k, i) in self.known(name) if (k, i) not in keep]
        self.con.executemany("DELETE FROM items WHERE account=? AND kind=? AND id=?",
                             [(name, k, i) for k, i in gone])
        self.con.commit()
        return len(gone)

    def mark_done(self, name: str, kind: str, item_id: str, now: datetime):
        self.con.execute("INSERT OR IGNORE INTO done_marks VALUES(?,?,?,?)", (name, kind, item_id, now.isoformat()))
        self.con.commit()

    def done_keys(self, name: str) -> set:
        rows = self.con.execute("SELECT kind, item_id FROM done_marks WHERE account=?", (name,))
        keys = {(r["kind"], r["item_id"]) for r in rows}
        rows = self.con.execute("SELECT item_id FROM solutions WHERE account=? AND status='submitted'", (name,))
        ids = [r["item_id"] for r in rows]          # assignment ids are hex, exam ids numeric: no clash
        return keys | {("assignment", i) for i in ids} | {("exam", i) for i in ids}

    def old_msgs(self, chat_id: int, before: datetime) -> list[int]:
        rows = self.con.execute("SELECT message_id FROM msgs WHERE chat_id=? AND ts<? ORDER BY message_id",
                                (chat_id, before.isoformat()))
        return [r["message_id"] for r in rows]
