"""Standby mode: GitHub Actions covers for the laptop only while the laptop is off.

The laptop stays the real bot (it answers Telegram, reads Madrasati, solves). While it runs it
 - sets the GitHub repo variable PAPAYA_LAST_BEAT (a "still alive" time), and
 - uploads an ENCRYPTED copy of its database to a release asset (state.db.enc).
The scheduled Action first checks that beat. Fresh beat: it stops there, doing nothing. Silent for
STANDBY_AFTER_MIN minutes: it downloads the copy and sends the reminders for deadlines the laptop
already knew about (it never logs in to Madrasati and never reads Telegram), then uploads which
reminders it sent (sent.db.enc). When the laptop is back it merges those, so nothing is sent twice.

The key (PAPAYA_STATE_KEY, a long random string) must be the same on the laptop (.env) and in the
repo's Actions secrets. Everything uploaded is sealed with it: encrypted, and checked for tampering.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .cycle import run_cycle
from .db import DB
from .parser import Item, RIYADH

STATE_ASSET = "state.db.enc"        # laptop -> Actions: the whole database
SENT_ASSET = "sent.db.enc"          # Actions -> laptop: which reminders Actions sent
RELEASE_TAG = "standby"
BEAT_VAR = "PAPAYA_LAST_BEAT"
AFTER_MIN = 45                      # silence that makes Actions take over
BEAT_EVERY = 5 * 60                 # laptop: seconds between beats
PUSH_EVERY = 20 * 60                # laptop: seconds between database uploads
PULL_EVERY = 10 * 60                # laptop: seconds between looks for Actions' sent list
MIN_KEY = 24
MAGIC = b"PPY1"


class StateError(Exception):
    pass


# ------------------------------------------------------------------ sealing (encrypt + check)
def _mac_key(key: str) -> bytes:
    return hashlib.sha256(b"papaya-mac|" + key.encode()).digest()


def _openssl(args: list[str], data: bytes, key: str) -> bytes:
    env = {**os.environ, "PAPAYA_STATE_KEY": key}
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                        "-pass", "env:PAPAYA_STATE_KEY", *args],
                       input=data, env=env, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise StateError("openssl failed: " + r.stderr.decode(errors="ignore")[:120])
    return r.stdout


def _check_key(key: str | None) -> str:
    if not key or len(key) < MIN_KEY:
        raise StateError(f"PAPAYA_STATE_KEY must be at least {MIN_KEY} characters")
    return key


def seal(data: bytes, key: str | None) -> bytes:
    key = _check_key(key)
    ct = _openssl(["-salt"], gzip.compress(data), key)
    mac = hmac.new(_mac_key(key), MAGIC + ct, hashlib.sha256).digest()
    return MAGIC + mac + ct


def unseal(blob: bytes, key: str | None) -> bytes:
    key = _check_key(key)
    if blob[:4] != MAGIC or len(blob) < 37:
        raise StateError("not a papaya state file")
    mac, ct = blob[4:36], blob[36:]
    good = hmac.new(_mac_key(key), MAGIC + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, good):
        raise StateError("wrong key or damaged file")
    return gzip.decompress(_openssl(["-d"], ct, key))


# ------------------------------------------------------------------ database copies
def snapshot(db_path: str) -> bytes:
    """A consistent copy of the live database (safe while the bot is writing to it)."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "s.db")
        src, dst = sqlite3.connect(db_path, timeout=30), sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            src.close(); dst.close()
        return Path(out).read_bytes()


def restore(db_path: str, data: bytes):
    """Replace the database file with this copy (for Actions, where nothing else uses it)."""
    tmp = db_path + ".incoming"
    Path(tmp).write_bytes(data)
    check = sqlite3.connect(tmp)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StateError("downloaded database is damaged")
    finally:
        check.close()
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass
    os.replace(tmp, db_path)


def merge_sent(db_path: str, other: bytes) -> int:
    """Copy 'reminder already sent' records from another copy into this database. Returns how many
    were new. Only that table: nothing else of the other copy is trusted or used."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "o.db")
        Path(p).write_bytes(other)
        con = sqlite3.connect(db_path, timeout=30)
        try:
            before = con.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
            con.execute("ATTACH DATABASE ? AS o", (p,))
            con.execute("INSERT OR IGNORE INTO sent(account,kind,item_id,event,sent_at) "
                        "SELECT account,kind,item_id,event,sent_at FROM o.sent")
            con.commit()
            return con.execute("SELECT COUNT(*) FROM sent").fetchone()[0] - before
        finally:
            con.close()


def _meta_of(data: bytes, k: str):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "m.db")
        Path(p).write_bytes(data)
        con = sqlite3.connect(p)
        try:
            r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return r[0] if r else None
        finally:
            con.close()


def _item(r) -> Item:
    return Item(kind=r["kind"], id=r["id"], title=r["title"] or "", subject=r["subject"] or "",
                deadline=datetime.fromisoformat(r["deadline"]), status=r["status"] or "",
                solve_url=r["solve_url"],
                opens_at=datetime.fromisoformat(r["opens_at"]) if r["opens_at"] else None)


# ------------------------------------------------------------------ transport: a GitHub release
class GhRelease:
    """Release assets through the `gh` CLI. A release is used because Actions can read and write it
    with its own token, and the laptop already has `gh` logged in (it sets the secrets)."""

    def __init__(self, cwd: str | None = None, run=subprocess.run):
        self.cwd, self.run = cwd or os.environ.get("PAPAYA_HOME", "."), run
        self.repo = os.environ.get("GITHUB_REPOSITORY")

    def _gh(self, *args, timeout=60):
        cmd = ["gh", *args] + (["-R", self.repo] if self.repo and args[0] in ("release", "variable") else [])
        return self.run(cmd, cwd=self.cwd, capture_output=True, timeout=timeout)

    def _ensure(self):
        if self._gh("release", "view", RELEASE_TAG).returncode != 0:
            self._gh("release", "create", RELEASE_TAG, "--title", "standby state",
                     "--notes", "Encrypted state for the standby check. Safe to delete; it is rebuilt.",
                     "--prerelease")

    def put(self, name: str, data: bytes):
        self._ensure()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / name
            p.write_bytes(data)
            r = self._gh("release", "upload", RELEASE_TAG, str(p), "--clobber", timeout=120)
        if r.returncode != 0:
            raise StateError("upload failed: " + r.stderr.decode(errors="ignore")[:120])

    def get(self, name: str):
        with tempfile.TemporaryDirectory() as d:
            r = self._gh("release", "download", RELEASE_TAG, "-p", name, "-D", d, "--clobber", timeout=120)
            p = Path(d) / name
            return p.read_bytes() if r.returncode == 0 and p.exists() else None

    def stamp(self, name: str):
        r = self._gh("release", "view", RELEASE_TAG, "--json", "assets",
                     "-q", f'.assets[] | select(.name=="{name}") | .updatedAt')
        out = r.stdout.decode().strip() if r.returncode == 0 else ""
        return out or None

    def set_var(self, name: str, value: str) -> bool:
        return self._gh("variable", "set", name, "--body", value).returncode == 0


# ------------------------------------------------------------------ laptop side
_last = {"beat": 0.0, "push": 0.0, "pull": 0.0}
_warned = {"nokey": False}
_lock = threading.Lock()


def tick(db_path: str, transport=None, now: float | None = None, key: str | None = None) -> list[str]:
    """Called by the main loop. Best effort and throttled: beat, upload the database copy, merge the
    reminders Actions sent while the laptop was off. Returns what it did (for logs and tests)."""
    key = key if key is not None else os.environ.get("PAPAYA_STATE_KEY")
    if not key:
        if not _warned["nokey"]:
            _warned["nokey"] = True
            print("standby: PAPAYA_STATE_KEY not set, GitHub Actions backup is off", flush=True)
        return []
    t = transport or GhRelease()
    now = time.time() if now is None else now
    did = []
    with _lock:
        if now - _last["beat"] >= BEAT_EVERY:
            ok = t.set_var(BEAT_VAR, str(int(now)))
            _last["beat"] = now if ok else now - BEAT_EVERY + 60     # a failure is retried in a minute
            did.append("beat" if ok else "beat-failed")
        if now - _last["pull"] >= PULL_EVERY:
            _last["pull"] = now
            did += _pull_sent(db_path, t, key)
        if now - _last["push"] >= PUSH_EVERY:
            _last["push"] = now
            t.put(STATE_ASSET, seal(snapshot(db_path), key))
            did.append("push")
    return did


def _pull_sent(db_path: str, t, key: str) -> list[str]:
    db = DB(db_path)
    stamp = t.stamp(SENT_ASSET)
    if not stamp or stamp == db.get_meta("standby_merged"):
        return []
    blob = t.get(SENT_ASSET)
    if not blob:
        return []
    n = merge_sent(db_path, unseal(blob, key))
    db.set_meta("standby_merged", stamp)
    if n:
        db.log(None, f"الاحتياطي على GitHub أرسل {n} تذكير وأنا طافي، ما راح أكررها 🛟")
    return [f"merged:{n}"]


def tick_async(db_path: str):
    """Never let GitHub being slow or down hold up the reminder loop."""
    def run():
        try:
            for what in tick(db_path):
                print(f"standby: {what}", flush=True)
        except Exception as e:
            print(f"standby failed: {type(e).__name__}: {str(e)[:100]}", flush=True)
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ Actions side
def run_standby(db_path: str, notifier, anotify, transport=None, now: datetime | None = None,
                beat: str | None = None, after_min: int = AFTER_MIN, key: str | None = None) -> str:
    now = now or datetime.now(RIYADH)
    beat = beat if beat is not None else os.environ.get(BEAT_VAR, "")
    if not beat.strip().isdigit():
        return "standby: the laptop never reported, nothing to do"
    age = now.timestamp() - int(beat)
    if age < after_min * 60:
        return f"standby: laptop alive ({int(age // 60)} min ago), nothing to do"
    key = key if key is not None else os.environ.get("PAPAYA_STATE_KEY")
    t = transport or GhRelease()
    state = t.get(STATE_ASSET)
    if not state:
        return "standby: no database copy uploaded yet, nothing to do"
    restore(db_path, unseal(state, key))
    old = t.get(SENT_ASSET)                  # what earlier standby runs already sent
    told = None
    if old:
        older = unseal(old, key)
        merge_sent(db_path, older)
        told = _meta_of(older, "standby_told")
    db = DB(db_path)
    if told:
        db.set_meta("standby_told", told)    # which outage was already announced survives between runs
    since = datetime.fromtimestamp(int(beat), RIYADH)
    if db.get_meta("standby_told") != beat:  # one notice per outage, not one per run
        db.set_meta("standby_told", beat)
        anotify(f"اللابتوب ساكت من {since:%m-%d %H:%M} 🛟\n"
                "الاحتياطي على GitHub يذكّر بالمواعيد اللي أعرفها بس (ما يقرأ واجبات جديدة ولا يرد على الأزرار) "
                "لين يرجع اللابتوب.")
    sent = 0
    for r in db.accounts():
        acct = r["name"]
        if db.chat_id(acct) is None:
            continue
        items = [_item(x) for x in db.upcoming(acct, now, limit=500)]
        sent += run_cycle(db, acct, items, notifier, now)
    # always put the copy back: it carries what was sent, so the next run and the laptop know
    t.put(SENT_ASSET, seal(snapshot(db_path), key))
    return f"standby: laptop silent {int(age // 60)} min, sent {sent} reminder(s)"
