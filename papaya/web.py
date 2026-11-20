"""Babaya web app (PWA) backend.

A small JSON API that runs inside `python -m papaya.main run`, next to the Telegram bot, and uses the
same database, solver and bot methods, so the web app behaves exactly like the bot. The phone talks
to the PWA on Vercel; Vercel's /api/bot proxy forwards to this server through a tunnel
(ngrok / Cloudflare). Only requests that carry PAPAYA_WEB_SECRET (added by the proxy) are served.

Pieces:
  * login with a fixed per-device code the admin generates (stored hashed), remembered per phone
  * the same lists, solve/submit, settings and admin switches as the bot
  * web push: every Telegram message the bot sends to a device is mirrored as a push notification
    (and into the web chat), and messages to "web:<device>" go to the web app only
  * while the laptop is off, Vercel queues requests; this server takes them when it wakes up

Run alone for a quick try:  python -m papaya.web code papaya01   (prints a login code)
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .db import DB
from .parser import RIYADH, SITE
from .reminders import HORIZON_AR, fmt_left
from .service import AUTO_SOLVE_BEFORE, budget_usd

URGENT = timedelta(hours=5)          # red "مستعجل" on the phone

ADMIN = "@admin"                     # the admin's web identity (not a device)
WEB = "web:"                         # chat ids for the web app: "web:papaya01"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I
CODE_LEN = 6
MAX_BODY = 64 * 1024
LOGIN_TRIES, LOGIN_WINDOW = 8, 15 * 60          # per client, per window
REQ_PER_MIN = 120                                 # per session
DONE_WORDS = ("تم التسليم", "تم الحل", "مكتمل", "تم تسليم")
SETTING_KEYS = {"notify_new", "notify_remind", "remind_external", "auto_solve", "auto_submit", "direct_submit"}
SOLVER_KEYS = {"auto_solve", "auto_submit", "direct_submit"}

WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_sessions(
  token TEXT PRIMARY KEY, account TEXT NOT NULL, created TEXT NOT NULL, seen TEXT NOT NULL, agent TEXT);
CREATE TABLE IF NOT EXISTS web_push(
  endpoint TEXT PRIMARY KEY, account TEXT NOT NULL, sub TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_inbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, ts TEXT NOT NULL,
  who TEXT NOT NULL, text TEXT NOT NULL, extra TEXT);
"""


def now_riyadh() -> datetime:
    return datetime.now(RIYADH)


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def ensure(db: DB) -> DB:
    db.con.executescript(WEB_SCHEMA)
    return db


def plain(text: str) -> str:
    """Telegram HTML -> plain text for pushes and the web chat."""
    t = re.sub(r"<br\s*/?>", "\n", text or "")
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def norm(s: str) -> str:
    """Arabic text for matching: no diacritics, one form of alef/ya/ta marbuta."""
    s = re.sub(r"[ً-ْـ]", "", (s or "").lower())
    return s.translate(str.maketrans("أإآىة", "ااايه")).strip()


# ---- codes & sessions -------------------------------------------------------------
def devices(db: DB) -> list[str]:
    return [r["name"] for r in db.accounts()]


def new_code(db: DB, account: str) -> str:
    """A fresh login code for a device (or ADMIN). The old code stops working; phones stay logged in."""
    taken = {db.get_meta(f"webcode:{a}") for a in devices(db) + [ADMIN]}
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if _h(code) not in taken:
            break
    db.set_meta(f"webcode:{account}", _h(code))
    return code


def clean_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def login(db: DB, code: str, agent: str = "") -> tuple[str, str] | None:
    """(account, session token) or None. The token is kept on the phone; only its hash is stored."""
    code = clean_code(code)
    if len(code) != CODE_LEN:
        return None
    want = _h(code)
    for acct in devices(db) + [ADMIN]:
        have = db.get_meta(f"webcode:{acct}")
        if have and hmac.compare_digest(have, want):
            token = secrets.token_urlsafe(32)
            ts = now_riyadh().isoformat(timespec="seconds")
            db.con.execute("INSERT INTO web_sessions VALUES(?,?,?,?,?)", (_h(token), acct, ts, ts, agent[:120]))
            db.con.commit()
            db.log(None if acct == ADMIN else acct, "دخول من الموقع 🌐")
            return acct, token
    return None


def session(db: DB, token: str) -> str | None:
    return session_by_hash(db, _h(token)) if token else None


def session_by_hash(db: DB, th: str) -> str | None:
    """The queue on Vercel keeps only the token's hash, never the token itself."""
    r = db.con.execute("SELECT account FROM web_sessions WHERE token=?", (th,)).fetchone()
    if not r:
        return None
    acct = r["account"]
    if acct != ADMIN and acct not in devices(db):
        return None
    db.con.execute("UPDATE web_sessions SET seen=? WHERE token=?", (now_riyadh().isoformat(timespec="seconds"), th))
    db.con.commit()
    return acct


def logout(db: DB, token: str):
    db.con.execute("DELETE FROM web_sessions WHERE token=?", (_h(token),))
    db.con.commit()


def logout_all(db: DB, account: str):
    db.con.execute("DELETE FROM web_sessions WHERE account=?", (account,))
    db.con.commit()


# ---- inbox (the web chat) -------------------------------------------------------------
def buttons_of(markup) -> list[dict]:
    """Telegram inline keyboard -> web actions (solve/submit/drop/submitall/link)."""
    out = []
    for row in (markup or {}).get("inline_keyboard", []):
        for b in row:
            label = plain(b.get("text", ""))
            if b.get("url"):
                out.append({"label": label, "url": b["url"]})
                continue
            act, _, arg = (b.get("callback_data") or "").partition(":")
            if act in ("solve", "submit", "drop", "submitall"):
                out.append({"label": label, "action": act, "id": arg})
    return out


def inbox_add(db: DB, account: str, who: str, text: str, extra: dict | None = None) -> int:
    cur = db.con.execute("INSERT INTO web_inbox(account,ts,who,text,extra) VALUES(?,?,?,?,?)",
                         (account, now_riyadh().isoformat(timespec="seconds"), who, text,
                          json.dumps(extra, ensure_ascii=False) if extra else None))
    db.con.execute("DELETE FROM web_inbox WHERE account=? AND id <= (SELECT MAX(id) FROM web_inbox) - 2000",
                   (account,))
    db.con.commit()
    return int(cur.lastrowid)


def inbox(db: DB, account: str, after: int = 0, limit: int = 100) -> list[dict]:
    rows = db.con.execute("SELECT * FROM web_inbox WHERE account=? AND id>? ORDER BY id DESC LIMIT ?",
                          (account, after, limit)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "who": r["who"], "text": r["text"],
             **(json.loads(r["extra"]) if r["extra"] else {})} for r in reversed(rows)]


# ---- push -----------------------------------------------------------------------------
def vapid(db: DB):
    """(Vapid object, public key for the browser) — made once and kept in the database."""
    try:
        from py_vapid import Vapid01, b64urlencode
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return None, None
    pem = db.get_meta("vapid_private")
    if pem:
        v = Vapid01.from_pem(pem.encode())
    else:
        v = Vapid01()
        v.generate_keys()
        db.set_meta("vapid_private", v.private_pem().decode())
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return v, b64urlencode(raw)


def subscribe(db: DB, account: str, sub: dict):
    ep = sub.get("endpoint", "")
    if not ep.startswith("https://") or not (sub.get("keys") or {}).get("p256dh"):
        raise ValueError("bad subscription")
    db.con.execute("INSERT INTO web_push VALUES(?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET account=excluded.account, "
                   "sub=excluded.sub", (ep, account, json.dumps(sub), now_riyadh().isoformat(timespec="seconds")))
    db.con.commit()


def unsubscribe(db: DB, endpoint: str):
    db.con.execute("DELETE FROM web_push WHERE endpoint=?", (endpoint,))
    db.con.commit()


def subs(db: DB, account: str) -> list[dict]:
    return [json.loads(r["sub"]) for r in db.con.execute("SELECT sub FROM web_push WHERE account=?", (account,))]


def _webpush(sub, data, v):          # replaced in tests
    from pywebpush import webpush
    contact = os.environ.get("PAPAYA_WEB_CONTACT", "mailto:babaya@example.com")
    webpush(sub, data, vapid_private_key=v, vapid_claims={"sub": contact}, ttl=6 * 3600, timeout=15)


def push(db: DB, account: str, body: str, title: str = "ببايه", url: str = "/", tag: str | None = None) -> int:
    """Send to every phone of this account that allowed notifications. Returns how many got it."""
    targets = subs(db, account)
    if not targets:
        return 0
    v, _ = vapid(db)
    if v is None:
        return 0
    data = json.dumps({"title": title, "body": body[:240], "url": url, "tag": tag}, ensure_ascii=False)
    sent = 0
    for sub in targets:
        try:
            _webpush(sub, data, v)
            sent += 1
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):               # the phone removed the app / turned notifications off
                unsubscribe(db, sub.get("endpoint", ""))
    return sent


def _bg(fn, *args):
    threading.Thread(target=lambda: _safe(fn, *args), daemon=True).start()


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception as e:
        print(f"web: {fn.__name__} failed: {type(e).__name__}: {e}", flush=True)


# ---- mirror the bot's Telegram messages ---------------------------------------------------
_installed = False
_progress_silent = threading.local()


def install(db_path: str):
    """Make every TelegramNotifier also feed the web app:
       * chat "web:<device>"  -> web chat + push only (never Telegram)
       * a real device chat   -> Telegram as before, plus a push and a copy in that device's web chat
    Silent (progress) messages are not pushed."""
    global _installed
    if _installed:
        return
    _installed = True
    from . import notifier as nmod
    from .env import admin_id
    orig_send, orig_delete = nmod.TelegramNotifier.send, nmod.TelegramNotifier.delete

    def send(self, chat_id, text, markup=None, silent=False):
        if isinstance(chat_id, str) and chat_id.startswith(WEB):
            return web_send(DB(db_path), chat_id[len(WEB):], text, markup, silent)
        mid = orig_send(self, chat_id, text, markup, silent)
        if mid:
            _bg(_mirror, db_path, chat_id, text, markup, silent, admin_id())
        return mid

    def delete(self, chat_id, ids):
        if isinstance(chat_id, str) and chat_id.startswith(WEB):
            db = DB(db_path)
            for i in ids:
                db.con.execute("DELETE FROM web_inbox WHERE id=? AND account=?", (i, chat_id[len(WEB):]))
            db.con.commit()
            return None
        return orig_delete(self, chat_id, ids)

    nmod.TelegramNotifier.send = send
    nmod.TelegramNotifier.delete = delete

    orig_edit = getattr(nmod.TelegramNotifier, "edit", None)
    if orig_edit is not None:          # the owner's live status message (one message edited in place)
        def edit(self, chat_id, message_id, text, markup=None):
            final = bool(markup) or "⏳" not in (text or "")
            if isinstance(chat_id, str) and chat_id.startswith(WEB):
                web_edit(DB(db_path), chat_id[len(WEB):], message_id, text, markup, final)
                return True
            ok = orig_edit(self, chat_id, message_id, text, markup)
            if ok and final:               # the finished result: the phone hears about it too
                _bg(_mirror, db_path, chat_id, text, markup, False, admin_id())
            return ok
        nmod.TelegramNotifier.edit = edit


def web_send(db: DB, account: str, text: str, markup=None, silent=False) -> int:
    ensure(db)
    btns = buttons_of(markup)
    mid = inbox_add(db, account, "bot", plain(text), {"buttons": btns, "progress": True} if silent and not btns
                    else {"buttons": btns} if btns else None)
    if not silent:
        push(db, account, plain(text), url="/#/chat")
    return mid


def web_edit(db: DB, account: str, mid: int, text: str, markup=None, final: bool = False):
    btns = buttons_of(markup)
    extra = {"buttons": btns} if btns else ({"progress": True} if not final else None)
    db.con.execute("UPDATE web_inbox SET text=?, extra=? WHERE id=? AND account=?",
                   (plain(text), json.dumps(extra, ensure_ascii=False) if extra else None, mid, account))
    db.con.commit()
    if final:
        push(db, account, plain(text), url="/#/chat")


def _mirror(db_path, chat_id, text, markup, silent, admin_chat):
    db = ensure(DB(db_path))
    acct = db.account_for_chat(chat_id) if isinstance(chat_id, int) else None
    if acct is None and admin_chat is not None and chat_id == admin_chat:
        acct = ADMIN
    if acct is None:
        return
    btns = buttons_of(markup)
    inbox_add(db, acct, "bot", plain(text), {"buttons": btns} if btns else None)
    if not silent:
        push(db, acct, plain(text), url="/#/chat")


# ---- what the app shows ----------------------------------------------------------------------
LETTERS = "أبجدهوزحطي"
TRUE_FALSE = {"صح", "صحيح", "خطا", "خاطيء", "خاطئ", "true", "false"}


def _answer_text(q: dict, a: dict) -> str:
    opts = q.get("options") or []
    pick = lambda n: (opts[n - 1] if n and 0 < n <= len(opts) else "؟")
    kind = q.get("kind")
    if kind == "choice" and a.get("choice") is not None:
        n = a["choice"]
        txt = pick(n)
        if len(opts) == 2 and all(norm(o).strip(" .") in TRUE_FALSE for o in opts):
            return txt                                             # true / false: just the word
        return f"{LETTERS[n - 1] if 0 < n <= len(LETTERS) else n}) {txt}"
    if kind == "match" and a.get("seq"):
        return " · ".join(f"{p} ← {pick(x)}" for p, x in zip(q.get("prompts", []), a["seq"]))
    if kind == "order" and a.get("seq"):
        return " ← ".join(pick(x) for x in a["seq"])
    if a.get("text"):
        return a["text"]
    return "حلّه بنفسك"


def solution_view(db: DB, account: str, item_id: str) -> dict | None:
    sol = db.solution(account, item_id)
    if not sol:
        return None
    data = json.loads(sol["data"])
    qs = {q["id"]: q for q in data.get("questions", [])}
    answers = []
    for n, a in enumerate(data.get("answers", []), 1):
        q = qs.get(a.get("qid"), {"text": "", "options": [], "kind": "choice"})
        answers.append({"n": n, "q": q.get("text", ""), "answer": _answer_text(q, a), "sure": bool(a.get("agree"))})
    return {"status": sol["status"], "all_agree": bool(sol["all_agree"]), "answers": answers,
            "updated": sol["updated_at"]}


def _abs(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else SITE.rstrip("/") + "/" + url.lstrip("/")


def items_view(db: DB, solver, account: str, now: datetime) -> list[dict]:
    done = db.done_keys(account)
    solve_ok, exam_ok = db.solver_enabled(account, now), db.solver_granted(account, now)
    out = []
    for r in db.upcoming(account, now, limit=80):
        status = r["status"] or ""
        if (r["kind"], r["id"]) in done or any(w in status for w in DONE_WORDS):
            continue
        dl = datetime.fromisoformat(r["deadline"])
        opened, external = status.strip() != "منتظر", "قنوات أخرى" in status
        row_ok = solver is not None and solver._check_item(db, account, r["id"])[0] is not None
        can = row_ok and (exam_ok if r["kind"] == "exam" else solve_ok)
        sol = db.solution(account, r["id"])
        out.append({
            "kind": r["kind"], "id": r["id"], "title": r["title"] or "", "subject": r["subject"] or "",
            "deadline": r["deadline"], "left": fmt_left(dl - now),
            "urgent": dl - now <= URGENT, "opened": opened, "external": external,
            "url": _abs(r["solve_url"]) if opened and not external else None,
            "can_solve": bool(can), "solution": sol["status"] if sol else None,
        })
    return out


def state(db: DB, solver, account: str, viewer: str) -> dict:
    now = now_riyadh()
    s = db.get_settings(account)
    items = items_view(db, solver, account, now)
    solve_ok = db.solver_enabled(account, now)
    until = db.solver_until(account)
    return {
        "device": account, "admin": viewer == ADMIN, "awake": True,
        "checked": db.get_meta(f"last_ok:{account}") or db.get_meta("last_cycle"),
        "error": db.get_meta(f"err:{account}") or None,
        "items": items,
        "counts": {"assignment": sum(i["kind"] == "assignment" for i in items),
                   "exam": sum(i["kind"] == "exam" for i in items)},
        "solver": {"homework": solve_ok, "exams": db.solver_granted(account, now),
                   "auto_hours": round(AUTO_SOLVE_BEFORE.total_seconds() / 3600),
                   "until": until.isoformat() if until else None},
        "settings": {
            "horizon_days": s.horizon_days, "horizons": HORIZON_AR,
            "remind_hours": s.remind_hours, "quiet_start": s.quiet_start, "quiet_end": s.quiet_end,
            **{k: getattr(s, k) for k in SETTING_KEYS if k not in SOLVER_KEYS or solve_ok},
        },
        "push": len(subs(db, account if viewer != ADMIN else ADMIN)),
    }


def admin_view(db: DB) -> dict:
    now = now_riyadh()
    out = []
    for r in db.accounts():
        n = r["name"]
        until = db.solver_until(n)
        out.append({
            "name": n, "linked": bool(r["chat_id"]), "granted": db.solver_granted(n, now),
            "homework": db.solver_enabled(n, now), "until": until.isoformat() if until else None,
            "error": db.get_meta(f"err:{n}") or None, "has_code": bool(db.get_meta(f"webcode:{n}")),
            "sessions": db.con.execute("SELECT COUNT(*) c FROM web_sessions WHERE account=?", (n,)).fetchone()["c"],
        })
    month = now.strftime("%Y-%m")
    return {"devices": out, "everyone": db.everyone_solves(), "spend": round(db.spend(month), 2),
            "budget": budget_usd(), "pending": len(db.pending_requests()),
            "logs": [dict(r) if not isinstance(r, dict) else r for r in db.logs(limit=15)]}


# ---- the chat: plain Arabic -> the same actions the buttons do ----------------------------------------
def _match_item(items: list[dict], words: str) -> dict | None:
    w = [x for x in norm(words).split() if len(x) > 1 and x not in ("واجب", "اختبار", "ال", "لي")]
    if not w:
        return None
    best, score = None, 0
    for it in items:
        hay = norm(it["subject"] + " " + it["title"])
        sc = sum(1 for x in w if x in hay or (x.startswith("ال") and x[2:] in hay))
        if sc > score:
            best, score = it, sc
    return best


def _list_reply(items: list[dict], when: str) -> tuple[str, list[dict]]:
    today = now_riyadh().date()
    pick = items
    if when == "today":
        pick = [i for i in items if datetime.fromisoformat(i["deadline"]).astimezone(RIYADH).date() == today]
    elif when == "tomorrow":
        pick = [i for i in items if datetime.fromisoformat(i["deadline"]).astimezone(RIYADH).date()
                == today + timedelta(days=1)]
    label = {"today": "اليوم", "tomorrow": "بكرة"}.get(when, "")
    if not pick:
        return (f"ما عندك شي {label} 🎉" if label else "ما عندك شي مفتوح 🎉"), []
    hw = sum(i["kind"] == "assignment" for i in pick)
    ex = len(pick) - hw
    parts = ([f"{hw} واجب"] if hw else []) + ([f"{ex} اختبار"] if ex else [])
    return f"عندك {' و'.join(parts)}{(' ' + label) if label else ''}:", pick[:8]


def chat_reply(db: DB, solver, account: str, text: str, run) -> dict:
    """Returns {"text", "items"?, "chips"?}. `run(action, arg)` starts a background job (same as a button)."""
    t = norm(text)
    items = items_view(db, solver, account, now_riyadh())
    chips = ["وش عندي اليوم؟", "وش عندي بكرة؟", "حل الكل", "حدّث"]
    if not t:
        return {"text": "اكتب لي وش تبغى 🙂", "chips": chips}
    if re.search(r"(^| )(هلا|مرحبا|السلام|هاي|اهلين)", t):
        return {"text": "هلا! أنا ببايه. أقدر أقول لك وش عندك، وأحل وأذكّرك.", "chips": chips}
    if "سلم الكل" in t or "سلم كل" in t:
        run("submitall", "-")
        return {"text": "تمام، أسلّم الحلول الجاهزة… ⏳"}
    if "حل الكل" in t or "حل كل" in t:
        if not db.solver_enabled(account):
            return {"text": "ميزة الحل مقفلة على جهازك."}
        run("solveall", "-")
        return {"text": "أبدأ أحل كل الواجبات المفتوحة… ⏳ أرسل لك لما أخلص."}
    m = re.match(r"^(حل|حلي|حللي|حل لي)\s+(.+)$", t)
    if m:
        it = _match_item(items, m.group(2))
        if not it:
            return {"text": "ما عرفت أي واحد تقصد. اختر من هنا:", "items": [i for i in items if i["can_solve"]][:6]}
        if not it["can_solve"]:
            return {"text": f"ما أقدر أحل «{it['subject'] or it['title']}» من هنا.", "items": [it]}
        run("solve", it["id"])
        return {"text": f"أحل «{it['subject'] or it['title']}» الحين… ⏳", "items": [it]}
    m = re.search(r"ذكرني بعد\s*(\d+)?\s*(ساعه|ساعتين|ساعات|دقيقه|دقايق|دقائق)", t)
    if m:
        n = int(m.group(1) or (2 if m.group(2) == "ساعتين" else 1))
        mins = n * 60 if m.group(2).startswith("ساع") else n
        mins = max(1, min(mins, 24 * 60))
        run("remind", str(mins))
        return {"text": f"أبشر، أذكّرك بعد {fmt_left(timedelta(minutes=mins))} ⏰"}
    if re.search(r"(حدث|تحديث|افحص)", t):
        run("refresh", "-")
        return {"text": "أفحص مدرستي الحين… 🔄"}
    if re.search(r"(بكره|بكرا)", t):
        txt, its = _list_reply(items, "tomorrow")
        return {"text": txt, "items": its}
    if "اليوم" in t:
        txt, its = _list_reply(items, "today")
        return {"text": txt, "items": its}
    if re.search(r"(عندي|واجب|اختبار|وش باقي|ايش باقي|القائمه)", t):
        txt, its = _list_reply(items, "all")
        return {"text": txt, "items": its}
    return {"text": "ما فهمت عليك 😅 جرّب وحدة من هذي:", "chips": chips}


# ---- the command line: the real Telegram bot, answering into the web page ---------------------------------
WEB_ONLY_BLOCKED = {"/start": "هذا الأمر لتيليجرام بس (ربط جهاز جديد).",
                    "/clear": "التنظيف لمحادثة تيليجرام بس. هنا اكتب cls.",
                    "/talk": "المحادثة مع جهاز تشتغل من تيليجرام بس."}
FAKE_MID = 10 ** 12          # message ids that never exist in Telegram (autoclean can never delete a real one)


class CaptureApi:
    """Stands in for the Telegram API: what the bot would send/edit/delete lands here, versioned so the
    page can ask "what changed since v?". Nothing reaches Telegram."""

    def __init__(self):
        self.lock = threading.Lock()
        self.msgs: dict = {}
        self.gone: dict = {}            # message id -> version it was deleted in
        self.seq, self.ver = FAKE_MID, 0

    def call(self, method: str, **p):
        with self.lock:
            if method == "sendMessage":
                self.seq += 1
                self.ver += 1
                self.msgs[self.seq] = {"id": self.seq, "text": p.get("text", ""), "kb": p.get("reply_markup"), "v": self.ver}
                return {"message_id": self.seq}
            if method in ("editMessageText", "editMessageReplyMarkup"):
                m = self.msgs.get(p.get("message_id"))
                if m:
                    self.ver += 1
                    if "text" in p:
                        m["text"] = p["text"]
                    m["kb"] = p.get("reply_markup") if "reply_markup" in p or method == "editMessageText" else m["kb"]
                    m["v"] = self.ver
                return {}
            if method in ("deleteMessage", "deleteMessages"):
                ids = p.get("message_ids") or [p.get("message_id")]
                self.ver += 1
                for i in ids:
                    if self.msgs.pop(i, None) is not None:
                        self.gone[i] = self.ver
                return True
            return {}                     # answerCallbackQuery, setMyCommands, reactions …

    def add_local(self, text: str):
        return self.call("sendMessage", text=text)

    def clear(self):
        with self.lock:
            self.ver += 1
            for i in list(self.msgs):
                self.gone[i] = self.ver
            self.msgs.clear()

    def since(self, v: int) -> dict:
        with self.lock:
            out = []
            for m in sorted(self.msgs.values(), key=lambda m: m["id"]):
                if m["v"] > v:
                    kb = m["kb"] or {}
                    out.append({"id": m["id"], "text": plain(m["text"]), "me": bool(m.get("me")),
                                "buttons": [[{"label": plain(b.get("text", "")), **({"url": b["url"]} if b.get("url")
                                             else {"data": b.get("callback_data", "")})} for b in row]
                                            for row in kb.get("inline_keyboard", [])],
                                "keys": [plain(b.get("text", "")) for row in kb.get("keyboard", []) for b in row]})
            return {"v": self.ver, "messages": out, "gone": [i for i, gv in self.gone.items() if gv > v]}


def cmd_chat(db: DB, viewer: str):
    """The Telegram chat this viewer is in the bot: the device's own chat, or the admin's."""
    from .env import admin_id
    return admin_id() if viewer == ADMIN else db.chat_id(viewer)


# ---- HTTP server ------------------------------------------------------------------------------------
class Limiter:
    def __init__(self):
        self.hits: dict = {}
        self.lock = threading.Lock()

    def allow(self, key, limit: int, window: float) -> bool:
        t = time.time()
        with self.lock:
            q = [x for x in self.hits.get(key, []) if t - x < window]
            if len(q) >= limit:
                self.hits[key] = q
                return False
            q.append(t)
            self.hits[key] = q
            return True


class App:
    """Everything the handler needs; built once by start()."""

    def __init__(self, db_path: str, solver=None, bot=None, refresh=None, secret: str | None = None):
        self.db_path = db_path
        self.solver = solver
        self.bot = bot
        self.refresh = refresh
        self.secret = secret if secret is not None else os.environ.get("PAPAYA_WEB_SECRET", "")
        self.limiter = Limiter()
        self.jobs: dict = {}             # (account, item_id) -> "working" | result
        self.lock = threading.Lock()
        self.cmd: dict = {}              # viewer -> (Bot answering into a CaptureApi, that CaptureApi)
        ensure(DB(db_path))

    def cmd_bot(self, viewer: str):
        with self.lock:
            if viewer not in self.cmd:
                from .bot import Bot
                from .env import admin_id
                cap = CaptureApi()
                db = self.db()
                bot = Bot(cap, db, admin_id(), devices(db), solver=self.solver, refresh=self.refresh,
                          db_factory=self.db)
                self.cmd[viewer] = (bot, cap)
            return self.cmd[viewer]

    def command(self, viewer: str, text: str | None = None, data: str | None = None, mid: int | None = None) -> None:
        """Run one command / button tap exactly as the Telegram bot would, answering into the page."""
        db = self.db()
        chat = cmd_chat(db, viewer)
        bot, cap = self.cmd_bot(viewer)
        if chat is None:
            cap.add_local("الأوامر تحتاج جهازك مربوط بتيليجرام أول." if viewer != ADMIN
                          else "حط PAPAYA_ADMIN_CHAT_ID في .env عشان تشتغل أوامر الأدمن.")
            return
        if text is not None:
            t = text.strip()
            first = t.split()[0].split("@")[0].lower() if t else ""
            if first in ("cls", "clear"):
                cap.clear()
                return
            if first in WEB_ONLY_BLOCKED:
                cap.add_local(WEB_ONLY_BLOCKED[first])
                return
            with cap.lock:
                cap.seq += 1
                cap.ver += 1
                cap.msgs[cap.seq] = {"id": cap.seq, "text": t, "kb": None, "v": cap.ver, "me": True}
            bot.handle({"message": {"chat": {"id": chat}, "from": {"id": chat}, "text": t}})
        elif data is not None:
            bot.handle({"callback_query": {"id": "web", "data": data, "from": {"id": chat},
                                           "message": {"chat": {"id": chat}, "message_id": int(mid or 0)}}})

    def db(self) -> DB:
        return DB(self.db_path)

    # background jobs, the same calls the Telegram buttons make
    def run(self, account: str, action: str, arg: str):
        chat = WEB + account
        key = (account, arg)
        with self.lock:                   # marked before the thread starts: the phone's first poll sees it
            self.jobs[key] = "working"

        def job():
            res = "error"
            try:
                if action == "solve":
                    res = self.solver.prepare(account, arg, chat)
                elif action == "submit":
                    res = self.solver.submit(account, arg, chat)
                elif action == "drop":
                    res = self.solver.drop(account, arg, chat)
                elif action == "solveall":
                    res = self.solver.solve_all(account, arg, chat)
                elif action == "submitall":
                    res = self.solver.submit_all(account, arg, chat)
                elif action == "refresh":
                    ok = self.refresh(account) if self.refresh else False
                    res = "ok" if ok else "error"
                    web_send(self.db(), account, "تم التحديث ✅" if ok else "ما قدرت أفحص المنصة الحين ⚠️")
                elif action == "remind":
                    time.sleep(int(arg) * 60)
                    web_send(self.db(), account, "⏰ هذا تذكيرك! شوف واجباتك.")
                    res = "ok"
            except Exception as e:
                print(f"web job {action} {account}: {type(e).__name__}: {e}", flush=True)
            finally:
                with self.lock:
                    self.jobs[key] = res

        threading.Thread(target=job, daemon=True).start()

    def job(self, account: str, arg: str):
        with self.lock:
            return self.jobs.get((account, arg))


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "babaya"
        sys_version = ""

        def log_message(self, fmt, *args):     # no tokens or codes in the log
            pass

        # -- helpers
        def _send(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > MAX_BODY:
                raise ValueError("too big")
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else {}

        def _client(self) -> str:
            return (self.headers.get("X-Client-IP") or self.client_address[0])[:64]

        def _token(self) -> str:
            a = self.headers.get("Authorization", "")
            return a[7:].strip() if a.startswith("Bearer ") else ""

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method: str):
            if not app.secret or not hmac.compare_digest(self.headers.get("X-Papaya-Secret", ""), app.secret):
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            path, qs = u.path.rstrip("/"), parse_qs(u.query)
            try:
                body = self._body() if method == "POST" else {}
            except ValueError:
                return self._send(400, {"error": "bad request"})
            db = app.db()
            try:
                return self._dispatch(method, path, qs, body, db)
            except PermissionError:
                return self._send(403, {"error": "forbidden"})
            except (KeyError, ValueError, TypeError):
                return self._send(400, {"error": "bad request"})
            except Exception as e:
                print(f"web: {method} {path} -> {type(e).__name__}: {e}", flush=True)
                return self._send(500, {"error": "server error"})

        def _dispatch(self, method, path, qs, body, db):
            if path == "/api/ping":
                return self._send(200, {"ok": True, "awake": True, "started": db.get_meta("started_at"),
                                        "vapid": vapid(db)[1]})
            if path == "/api/login" and method == "POST":
                ip = self._client()
                if not (app.limiter.allow(("login", ip), LOGIN_TRIES, LOGIN_WINDOW)
                        and app.limiter.allow(("login", "*"), LOGIN_TRIES * 5, LOGIN_WINDOW)):
                    return self._send(429, {"error": "كثرت المحاولات، جرّب بعد ربع ساعة"})
                got = login(db, str(body.get("code", "")), self.headers.get("User-Agent", ""))
                if not got:
                    return self._send(401, {"error": "الكود غلط"})
                return self._send(200, {"account": got[0], "token": got[1], "admin": got[0] == ADMIN})

            token = self._token()
            viewer = session(db, token)
            if not viewer:
                return self._send(401, {"error": "login"})
            if not app.limiter.allow(("req", token), REQ_PER_MIN, 60):
                return self._send(429, {"error": "شوي شوي 🙂"})
            acct = viewer
            if viewer == ADMIN:                 # the admin looks at one device at a time
                acct = self.headers.get("X-As") or (qs.get("as") or [""])[0] or (devices(db) or [ADMIN])[0]
                if acct not in devices(db):
                    raise ValueError("no such device")

            if path == "/api/logout" and method == "POST":
                logout(db, token)
                return self._send(200, {"ok": True})
            if path == "/api/state":
                st = state(db, app.solver, acct, viewer)
                if viewer == ADMIN:
                    st["devices"] = devices(db)
                return self._send(200, st)
            if path == "/api/solution":
                item_id = qs["id"][0]
                return self._send(200, {"job": app.job(acct, item_id), "solution": solution_view(db, acct, item_id)})
            if path in ("/api/solve", "/api/submit", "/api/drop") and method == "POST":
                item_id = str(body["id"])
                if not db.solver_enabled(acct):
                    raise PermissionError
                if app.job(acct, item_id) == "working":
                    return self._send(200, {"job": "working"})
                app.run(acct, path.rsplit("/", 1)[1], item_id)
                return self._send(200, {"job": "working"})
            if path in ("/api/solveall", "/api/submitall") and method == "POST":
                if not db.solver_enabled(acct):
                    raise PermissionError
                app.run(acct, path.rsplit("/", 1)[1], "-")
                return self._send(200, {"job": "working"})
            if path == "/api/refresh" and method == "POST":
                app.run(acct, "refresh", "-")
                return self._send(200, {"job": "working"})
            if path == "/api/done" and method == "POST":
                kind = body["kind"]
                if kind not in ("assignment", "exam"):
                    raise ValueError(kind)
                db.mark_done(acct, kind, str(body["id"]), now_riyadh())
                return self._send(200, {"ok": True})
            if path == "/api/settings" and method == "POST":
                self._settings(db, acct, body)
                return self._send(200, state(db, app.solver, acct, viewer)["settings"])
            if path == "/api/inbox":
                after = int((qs.get("after") or ["0"])[0])
                return self._send(200, {"messages": inbox(db, acct, after)})
            if path == "/api/chat" and method == "POST":
                text = str(body.get("text", ""))[:300]
                first = inbox_add(db, acct, "me", text)
                rep = chat_reply(db, app.solver, acct, text, lambda a, x: app.run(acct, a, x))
                inbox_add(db, acct, "bot", rep["text"], {k: rep[k] for k in ("items", "chips") if rep.get(k)} or None)
                return self._send(200, {"messages": inbox(db, acct, first - 1)})
            if path == "/api/cmd":
                bot_view = ADMIN if viewer == ADMIN else acct
                if method == "POST":
                    if "data" in body:
                        app.command(bot_view, data=str(body["data"])[:64], mid=int(body.get("id") or 0))
                    else:
                        app.command(bot_view, text=str(body.get("text", ""))[:300])
                    time.sleep(0.25)             # most answers are immediate; slow ones come with the next poll
                _, cap = app.cmd_bot(bot_view)
                return self._send(200, cap.since(int((qs.get("after") or [body.get("after", 0)])[0] or 0)))
            if path == "/api/push" and method == "POST":
                subscribe(db, ADMIN if viewer == ADMIN else acct, body["subscription"])
                return self._send(200, {"ok": True})
            if path == "/api/push/off" and method == "POST":
                unsubscribe(db, str(body["endpoint"]))
                return self._send(200, {"ok": True})
            if path == "/api/push/test" and method == "POST":
                n = push(db, ADMIN if viewer == ADMIN else acct, "هذا إشعار تجربة من ببايه 🦜", url="/")
                return self._send(200, {"sent": n})

            if not path.startswith("/api/admin"):
                return self._send(404, {"error": "not found"})
            if viewer != ADMIN:
                raise PermissionError
            if path == "/api/admin":
                return self._send(200, admin_view(db))
            if path == "/api/admin/everyone" and method == "POST":
                on = bool(body["on"])
                if app.bot:
                    app.bot.set_everyone_solves(on)
                else:
                    db.set_everyone(on)
                return self._send(200, admin_view(db))
            if path == "/api/admin/solver" and method == "POST":
                name = str(body["name"])
                if name not in devices(db):
                    raise ValueError(name)
                if body.get("on"):
                    hours = int(body.get("hours") or 0)
                    if app.bot:
                        app.bot.grant_solver(name, hours)
                    else:
                        db.set_solver(name, True, now_riyadh() + timedelta(hours=hours) if hours else None)
                elif app.bot:
                    app.bot.revoke_solver(name)
                else:
                    db.set_solver(name, False)
                return self._send(200, admin_view(db))
            if path == "/api/admin/code" and method == "POST":
                name = str(body["name"])
                if name not in devices(db) + [ADMIN]:
                    raise ValueError(name)
                code = new_code(db, name)
                db.log(None if name == ADMIN else name, "كود دخول جديد للموقع 🔑")
                return self._send(200, {"name": name, "code": code})
            if path == "/api/admin/logout" and method == "POST":
                name = str(body["name"])
                logout_all(db, name)
                return self._send(200, admin_view(db))
            return self._send(404, {"error": "not found"})

        def _settings(self, db, acct, body):
            solve_ok = db.solver_enabled(acct)
            for k, v in body.items():
                if k in SETTING_KEYS:
                    if k in SOLVER_KEYS and not solve_ok:
                        raise PermissionError
                    db.set_setting(acct, k, 1 if v else 0)
                elif k == "horizon_days":
                    if int(v) not in HORIZON_AR:
                        raise ValueError(k)
                    db.set_setting(acct, k, int(v))
                elif k == "remind_hours":
                    if float(v) not in (3, 6, 10, 24):
                        raise ValueError(k)
                    db.set_setting(acct, k, float(v))
                elif k == "quiet":
                    a, b = (int(x) for x in v)
                    if not (0 <= a < 24 and 0 <= b < 24):
                        raise ValueError(k)
                    db.set_setting(acct, "quiet_start", a)
                    db.set_setting(acct, "quiet_end", b)
                else:
                    raise KeyError(k)

    return H


# ---- requests queued on Vercel while the laptop was off ------------------------------------------------------
# ---- Upstash (the small store Vercel gives you): where the bot leaves its address and Vercel the queue ----
URL_KEY, QUEUE_KEY = "babaya:url", "babaya:queue"


def _upstash_env() -> tuple[str, str]:
    """Upstash's own names, or the KV_* names Vercel gives when it links the store."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL") or ""
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN") or ""
    return url.rstrip("/"), token


def upstash(commands: list, path: str = "/pipeline") -> list:
    base, token = _upstash_env()
    if not base or not token:
        raise RuntimeError("no upstash")
    req = urllib.request.Request(base + path, json.dumps(commands).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def has_upstash() -> bool:
    return all(_upstash_env())


# ---- the tunnel: started by the bot itself, nothing to set up ------------------------------------------------
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def tunnel_url(line: str) -> str | None:
    m = TUNNEL_RE.search(line or "")
    return m.group(0) if m else None


def _cloudflared(home: str) -> str | None:
    """cloudflared from PATH, or downloaded once next to the bot (from Cloudflare's GitHub releases)."""
    import platform
    import shutil
    found = shutil.which("cloudflared")
    if found:
        return found
    path = os.path.join(home, ".bin", "cloudflared")
    if os.path.exists(path):
        return path
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if not arch:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    print("web app: downloading cloudflared (one time)…", flush=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(path + ".part", "wb") as f:
        f.write(r.read())
    os.chmod(path + ".part", 0o755)
    os.replace(path + ".part", path)
    return path


def _tunnel_loop(port: int, home: str):
    """Keep a free Cloudflare quick tunnel open to the local API and tell Vercel (through Upstash) its address.
    The address changes on every start; Vercel always reads the latest one."""
    import subprocess
    while True:
        try:
            exe = _cloudflared(home)
            if not exe:
                print("web app: cloudflared is not available for this computer", flush=True)
                return
            proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                url = tunnel_url(line)
                if url:
                    upstash([["SET", URL_KEY, url]])
                    print(f"web app: online ({url})", flush=True)
            proc.wait()
        except Exception as e:
            print(f"web app: tunnel: {type(e).__name__}: {e}", flush=True)
        time.sleep(15)                          # it dropped: open a new one


def take_queue(app: App, fetch=None) -> int:
    """Pull and run what people asked for while the bot was asleep. Returns how many were handled."""
    url = os.environ.get("PAPAYA_WEB_QUEUE_URL", "")
    if fetch is None and has_upstash() and app.secret:
        def fetch():                            # straight from the store, no Vercel call
            out = upstash([["LRANGE", QUEUE_KEY, "0", "-1"], ["DEL", QUEUE_KEY]], "/multi-exec")
            items = []
            for s in (out[0].get("result") or []):
                try:
                    items.append(json.loads(s))
                except ValueError:
                    pass
            return items
    if fetch is None and (not url or not app.secret):
        return 0
    if fetch is None:
        def fetch():
            req = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"X-Papaya-Secret": app.secret, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r).get("items", [])
    items = fetch()
    db = app.db()
    n = 0
    for q in items:
        who = session_by_hash(db, str(q.get("th", "")))
        if not who:
            continue
        acct = who
        if who == ADMIN:
            acct = q.get("as") if q.get("as") in devices(db) else None
            if not acct:
                continue
        action, arg = q.get("action"), str(q.get("id") or "-")
        target = who
        if action in ("solve", "submit") and db.solver_enabled(acct):
            push(db, target, "صحيت! ✅ أشتغل على طلبك الحين…", url="/")
            app.run(acct, action, arg)
        elif action == "refresh":
            app.run(acct, "refresh", "-")
        else:                                   # "notify": just tell them the bot is back
            push(db, target, "ببايه صحي ✅ تقدر تكمل من الموقع.", url="/")
        n += 1
    return n


def _queue_loop(app: App):
    while True:
        try:
            take_queue(app)
        except Exception as e:
            print(f"web queue: {type(e).__name__}", flush=True)
        time.sleep(60)


def start(db_path: str, solver=None, bot=None, refresh=None) -> ThreadingHTTPServer | None:
    secret = os.environ.get("PAPAYA_WEB_SECRET", "")
    if len(secret) < 24:
        print("web app: off (put PAPAYA_WEB_SECRET in .env, at least 24 characters, to turn it on)", flush=True)
        return None
    app = App(db_path, solver, bot, refresh, secret)
    port = int(os.environ.get("PAPAYA_WEB_PORT", "8787"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=_queue_loop, args=(app,), daemon=True).start()
    print(f"web app: API on http://127.0.0.1:{port}", flush=True)
    if os.environ.get("PAPAYA_WEB_TUNNEL", "on") != "off":
        if has_upstash():
            home = os.environ.get("PAPAYA_HOME", ".")
            threading.Thread(target=_tunnel_loop, args=(port, home), daemon=True).start()
        else:
            print("web app: put UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN in .env (from Vercel → Storage)",
                  flush=True)
    return srv


def main(argv=None):
    import sys
    from .env import load_env
    load_env()
    argv = argv if argv is not None else sys.argv[1:]
    home = os.environ.get("PAPAYA_HOME", ".")
    db = ensure(DB(os.path.join(home, "papaya.db")))
    if len(argv) == 2 and argv[0] == "code":
        name = "@admin" if argv[1] in ("admin", "@admin") else argv[1]
        if name != ADMIN and name not in devices(db):
            sys.exit(f"no device named {name}. devices: {', '.join(devices(db))}")
        print(f"{name}: {new_code(db, name)}   (the old code for {name} no longer works)")
    elif argv[:1] == ["secret"]:
        print(secrets.token_urlsafe(32))
    else:
        sys.exit("usage: python -m papaya.web code <device|admin> | secret")


if __name__ == "__main__":
    main()
