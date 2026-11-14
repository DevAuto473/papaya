"""Telegram bot: linking, settings, /list, admin commands. Long-polling, stdlib only.

Passwords never pass through Telegram. Linking = user sends /start, admin taps a button."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from html import escape

from . import scraper
from .db import DB
from .parser import RIYADH
from .parser import Item
from .reminders import HORIZON_AR, digest, fmt_left, in_horizon, list_buttons, wanted


class ApiError(Exception):
    """Telegram answered with an error. str() carries Telegram's own reason (never the token)."""
    def __init__(self, method: str, code: int, description: str, retry_after: int = 0):
        super().__init__(f"{method}: HTTP {code} {description}".strip())
        self.code, self.description, self.retry_after = code, description, retry_after


def is_not_modified(e: Exception) -> bool:
    return "not modified" in str(e)


def is_network_error(e: Exception) -> bool:
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)) \
        and not isinstance(e, urllib.error.HTTPError)


def maybe_delivered(e) -> bool:
    """A timeout after the request left: Telegram may have shown the message already."""
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


class Api:
    RETRIES = 2                    # extra tries for a request that never reached Telegram

    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params):
        body = json.dumps(params).encode()
        for attempt in range(self.RETRIES + 1):
            req = urllib.request.Request(self.base + method, body, {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=params.get("timeout", 0) + 20) as r:
                    return json.load(r)["result"]
            except urllib.error.HTTPError as e:
                try:
                    data = json.load(e)
                except Exception:
                    data = {}
                err = ApiError(method, e.code, str(data.get("description", "")),
                               int((data.get("parameters") or {}).get("retry_after") or 0))
                if attempt < self.RETRIES and (e.code == 429 or e.code >= 500):
                    time.sleep(min(err.retry_after or 2 * (attempt + 1), 30))   # flood limit / Telegram hiccup
                    continue
                raise err from None
            except urllib.error.URLError as e:
                # connection / DNS / TLS-handshake failure: the request was never delivered, so a retry
                # cannot duplicate a message. (A read timeout is NOT retried: it may have been delivered.)
                # urllib wraps a timeout while waiting for Telegram's answer in URLError too: that one
                # may already be in the chat, so it is raised, never sent a second time.
                if maybe_delivered(e):
                    raise
                if attempt < self.RETRIES and method != "getUpdates":
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise


def _is_list_msg(msg) -> bool:
    """The /list message: its first line ends with 📋 (used to redraw it in place)."""
    return (msg.get("text") or "").split("\n", 1)[0].strip().endswith("📋")


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


HELP = (
    "استخدم الأزرار تحت مربع الكتابة 👇\n\n"
    "<b>الواجبات والاختبارات</b> — واجباتك واختباراتك القريبة 📋\n"
    "<b>الإعدادات</b> — متى يذكّرك، ساعات الهدوء، والحذف التلقائي ⚙️\n"
    "<b>تنظيف</b> — يحذف رسائل المحادثة 🧹\n\n"
    "أوامر إضافية: /remind 8 (التذكير قبل 8 ساعات) · /quiet 23 6 (ساعات الهدوء)\n"
    "اختفت الأزرار؟ أرسل /start"
)
ADMIN_HELP = ("\n\n<b>للمشرف:</b>\n/health — حالة النظام\n/as papaya02 — استخدم البوت كأنك صاحب الجهاز 👤 (/me للرجوع)\n/talk papaya02 — تكلّم مع صاحب الجهاز باسم البوت 💬 (/done للإنهاء)\nالإدارة — قائمة الأجهزة والصلاحيات والسجل 👑\n/name papaya03 أختي — تسمية جهاز\n/status — حالة الأجهزة والمصروف\n/unlink papaya01 — فك الربط\n"
              "/grant papaya01 3d — إعطاء ميزة الحل لمدة (12h, 3d, 2w — بدون مدة = دائم)\n/revoke papaya01 — سحب ميزة الحل")
SOLVER_HELP = "\n<b>حل كل الواجبات</b> — أو اضغط 🆘 جنب أي واجب في «الواجبات والاختبارات» 🆘"

GUIDE = (
    "<b>تم تسجيلك في المنصة، البوت شغّال لك!</b> 🎉\n\n"
    "<b>دليل سريع</b> 📖\n\n"
    "<b>وش يسوي البوت؟</b>\n"
    "يراقب واجباتك واختباراتك في مدرستي كل 10 دقايق، ويرسل لك:\n"
    "• لما ينزل شي جديد قريب 🆕\n"
    "• لما ينفتح واجب أو اختبار 🔓\n"
    "• تذكير قبل النهاية ⏳\n\n"
    "<b>الأزرار تحت</b> 👇\n"
    "<b>الواجبات والاختبارات</b> 📋 — اللي ينتهي قريب، وتتحدث لحالها\n"
    "<b>الإعدادات</b> ⚙️ — المدة، وقت التذكير، ساعات الهدوء\n"
    "<b>تنظيف</b> 🧹 — يمسح المحادثة\n\n"
    "<b>في الرسائل</b>\n"
    "اضغط اسم الواجب أو الاختبار يفتح لك على طول\n"
    "حليت اختبار؟ اضغط ✅ ورقمه ويختفي\n"
    "الواجبات تختفي لحالها أول ما تسلّمها\n\n"
    "<b>معلومات</b>\n"
    "• ما يزعجك من 11م إلى 6ص (تقدر تغيّرها)\n"
    "• الرسائل تنحذف لحالها بعد 24 ساعة\n"
    "• الأزرار اختفت؟ أرسل /start"
)


def guide(solver_on: bool) -> str:
    return GUIDE + ("\n\n<b>ميزة الحل:</b> زر 🆘 جنب الواجب يحله لك ويعرض الإجابات قبل التسليم" if solver_on else "")

BTN_LIST, BTN_SETTINGS, BTN_CLEAR, BTN_SOLVEALL, BTN_HELP, BTN_ADMIN = (
    "الواجبات والاختبارات 📋", "الإعدادات ⚙️", "تنظيف 🧹", "حل كل الواجبات 🆘", "مساعدة ❓", "الإدارة 👑")
BUTTON_CMDS = {BTN_LIST: "/list", BTN_SETTINGS: "/settings", BTN_CLEAR: "/clear",
               BTN_SOLVEALL: "/solveall", BTN_HELP: "/help", BTN_ADMIN: "/admin"}
BUTTON_CMDS.update({"📋 مواعيدي": "/list", "⚙️ الإعدادات": "/settings", "🧹 تنظيف": "/clear",   # keyboards sent
                    "🆘 حل كل الواجبات": "/solveall", "❓ مساعدة": "/help", "👑 الإدارة": "/admin"})  # before the change
COMMANDS = [("list", "الواجبات والاختبارات 📋"), ("settings", "الإعدادات ⚙️"), ("clear", "تنظيف المحادثة 🧹"),
            ("help", "مساعدة ❓")]
CLEAR_RANGE = 1500          # how far back /clear reaches by message number (Telegram: ≤48h old only)
CLEAR_COOLDOWN = 30         # seconds: a second /clear from the same chat this soon is ignored
STALE_TAP = 120             # seconds: a /clear older than this (backlog after a restart) is skipped
FLOOD_MAX = 20              # more messages than this from one chat (admin excluded) ...
FLOOD_WINDOW = 30           # ... within this many seconds ...
FLOOD_MUTE = 120            # ... and the bot ignores that chat for this many seconds


def menu(solver_on: bool) -> dict:
    """Persistent buttons under the text box, so nobody has to type commands."""
    rows = [[{"text": BTN_LIST}, {"text": BTN_SETTINGS}]]
    rows.append([{"text": BTN_SOLVEALL}, {"text": BTN_CLEAR}] if solver_on else [{"text": BTN_CLEAR}, {"text": BTN_HELP}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def _hour(h: int) -> str:
    return f"{h % 12 or 12}{'ص' if h < 12 else 'م'}"


def settings_view(db: DB, account: str):
    solver_on = db.solver_enabled(account)
    s = db.get_settings(account)
    quiet = "متوقفة" if s.quiet_start == s.quiet_end else f"{_hour(s.quiet_start)} → {_hour(s.quiet_end)}"
    onoff = lambda b: "✅" if b else "⬜"
    dot = lambda c: "● " if c else ""
    text = (f"إعدادات <b>{escape(account)}</b> ⚙️\n"
            f"• التنبيهات عن: المفتوح اللي ينتهي خلال {HORIZON_AR.get(s.horizon_days, s.horizon_days)}\n"
            f"• التذكير قبل: {s.remind_hours:g} ساعة\n• ساعات الهدوء: {quiet}")
    rows = [
        [(dot(s.horizon_days == d) + HORIZON_AR[d], f"h:{d}") for d in (1, 2)],
        [(dot(s.horizon_days == d) + HORIZON_AR[d], f"h:{d}") for d in (3, 7, 0)],
        [(dot(s.remind_hours == h) + f"{h}س ⏳", f"r:{h}") for h in (3, 6, 10, 24)],
        [("هدوء 11م–6ص", "q:23:6"), ("12ص–7ص", "q:0:7"), ("بدون هدوء", "q:0:0")],
        [(f"المواعيد القريبة {onoff(s.notify_new)}", "t:notify_new"), (f"التذكير {onoff(s.notify_remind)}", "t:notify_remind")],
        [(f"التسليم الخارجي 📨 {onoff(s.remind_external)}", "t:remind_external")],
    ]
    if solver_on:   # these rows do not exist at all for devices without the feature
        rows.append([(f"حل تلقائي قبل 5س 🤖 {onoff(s.auto_solve)}", "t:auto_solve"),
                     (f"تسليم تلقائي 📤 {onoff(s.auto_submit)}", "t:auto_submit")])
        rows.append([(f"سلّم مباشرة بدون معاينة ⚡ {onoff(s.direct_submit)}", "t:direct_submit")])
    rows.append([(f"احذف الرسائل بعد 24 ساعة 🧹 {onoff(s.autoclean)}", "t:autoclean")])
    return text, kb(rows)


LOADING: dict = {}                  # (account, item_id or "-") -> spinner label; shared by every Bot instance
SPIN = ("⏳", "⌛")



def _cmd_key(upd):
    """(chat, command) for a command message, else None. Used to collapse repeats in one batch."""
    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    text = BUTTON_CMDS.get(text, text)
    if not text.startswith("/"):
        return None
    return (msg.get("chat", {}).get("id"), text)


def _spawn_safe(fn, *args):
    """Run a button job in the background. A crash there is logged in one line instead of a
    traceback, and never takes anything else down."""
    def run():
        try:
            fn(*args)
        except Exception as e:
            print(f"background job {getattr(fn, '__name__', 'job')} failed: {type(e).__name__}: {e}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def parse_duration(t: str):
    """'3d' / '12h' / '2w' / '3' (days) -> hours. 0 = permanent. None if it can't be read."""
    t = (t or "").strip().lower()
    if t in ("", "0", "دائم", "forever"):
        return 0
    unit = {"h": 1, "d": 24, "w": 168, "m": 720}.get(t[-1])
    num = t[:-1] if unit else t
    try:
        n = float(num)
    except ValueError:
        return None
    h = int(round(n * (unit or 24)))
    return h if 0 < h <= 24 * 366 else None


def duration_ar(hours: int) -> str:
    if not hours:
        return "دائمة"
    if hours % 720 == 0:
        return f"{hours // 720} شهر"
    if hours % 168 == 0:
        return "أسبوع" if hours == 168 else f"{hours // 168} أسابيع"
    if hours % 24 == 0:
        return "يوم" if hours == 24 else f"{hours // 24} أيام"
    return f"{hours} ساعة"


def _sig(text, kb) -> str:
    import hashlib
    return hashlib.md5((text + json.dumps(kb or {}, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:12]

class Bot:
    def __init__(self, api, db: DB, admin_id: int | None, account_names: list[str],
                 solver=None, spawn=None, refresh=None, db_factory=None, profile_dir=None):
        self.api, self.db, self.admin_id, self.names = api, db, admin_id, account_names
        self.refresh = refresh            # refresh(account) -> bool : read the platform right now
        self.db_factory = db_factory      # a fresh connection for work done in a background thread
        self.profile_dir = profile_dir    # profile_dir(name) -> Path : enables fully-automatic signup login
        self._last_refresh: dict = {}
        self._signup: dict = {}           # chat -> answers so far. Memory only: the password never touches disk
        self.wipe_after = 3600            # seconds until the admin's copy of a password is deleted (None = never)
        self.solver = solver
        self.spawn = spawn or _spawn_safe
        self.clock = time.time            # replaced in tests
        self._guard = threading.Lock()
        self._hits: dict = {}             # chat -> times of its recent messages (flood guard)
        self._muted: dict = {}            # chat -> time its mute ends
        self._clearing: set = set()       # chats with a /clear running now
        self._last_clear: dict = {}       # chat -> time of its last /clear
        for n in account_names:
            db.ensure_account(n)

    # ---- protection against floods ------------------------------------
    def _flooding(self, chat) -> bool:
        """True while a chat (never the admin) is sending far more than a person would. The bot
        then ignores it for FLOOD_MUTE seconds and says so once, so one person can't freeze it."""
        if chat is None or chat == self.admin_id:
            return False
        now = self.clock()
        with self._guard:
            if now < self._muted.get(chat, 0):
                return True
            hits = [t for t in self._hits.get(chat, []) if t > now - FLOOD_WINDOW] + [now]
            self._hits[chat] = hits
            if len(hits) <= FLOOD_MAX:
                return False
            self._muted[chat] = now + FLOOD_MUTE
            self._hits.pop(chat, None)
        print(f"flood guard: chat {chat} muted for {FLOOD_MUTE}s", flush=True)
        try:
            self.say(chat, f"أرسلت رسائل كثيرة ورا بعض ⏳ بوقف أرد عليك {FLOOD_MUTE // 60} دقايق، وبعدها كمّل عادي.")
        except Exception:
            pass
        return True

    def _request_clear(self, chat, msg, account):
        """/clear runs in the background, one at a time per chat, never again within CLEAR_COOLDOWN,
        and taps left over from before a restart are skipped. So spamming it can't hang the bot."""
        now = self.clock()
        if msg.get("date") and now - msg["date"] > STALE_TAP:
            return
        with self._guard:
            if chat in self._clearing or now - self._last_clear.get(chat, float("-inf")) < CLEAR_COOLDOWN:
                return
            self._clearing.add(chat)
            self._last_clear[chat] = now

        def run():
            try:
                self._list_bot().on_clear(chat, msg.get("message_id"), account)
            finally:
                with self._guard:
                    self._clearing.discard(chat)
                    self._last_clear[chat] = self.clock()
        self.spawn(run)

    def say(self, chat, text, markup=None):
        p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup and "keyboard" in markup and chat == self.admin_id:      # the admin's menu has 👑
            markup = {**markup, "keyboard": markup["keyboard"] + [[{"text": BTN_ADMIN}]]}
        if markup:
            p["reply_markup"] = markup
        res = self.api.call("sendMessage", **p)
        try:
            self.db.log_msg(chat, res["message_id"], datetime.now(RIYADH))
        except Exception:
            pass
        return res

    def on_clear(self, chat, upto: int | None = None, account=None):
        """Delete this chat's messages: every one we logged (both sides) plus a sweep of the recent
        message numbers (private chats count 1, 2, 3...) for anything we didn't log.
        Telegram itself never lets a bot delete messages older than 48 hours."""
        from datetime import timedelta
        now = datetime.now(RIYADH)
        too_old = set(self.db.old_msgs(chat, now - timedelta(hours=48)))
        ids = set(self.db.chat_msgs(chat)) - too_old
        if upto:
            ids |= set(range(max(1, upto - CLEAR_RANGE), upto + 1)) - too_old
        self._delete_ids(chat, sorted(ids, reverse=True))     # newest first
        self.db.forget_msgs(chat)
        solver_on = bool(account and self.db.solver_enabled(account))
        note = ("\n<i>فيه رسائل أقدم من 48 ساعة، وتيليجرام ما يسمح لأي بوت يحذفها. "
                "امسحها مرة وحدة من ⋮ ← Clear history، وبعدها /clear يمسح كل شي "
                "(خصوصاً لو «احذف الرسائل بعد 24 ساعة» شغّال في الإعدادات).</i>") if too_old else ""
        self.say(chat, "نظّفت المحادثة 🧹" + note, menu(solver_on) if account else None)

    def _delete_ids(self, chat, ids: list[int], budget: int = 80) -> int:
        """deleteMessages takes 100 at a time. If Telegram refuses a batch, split it and try the
        halves, so one undeletable message never protects the rest. Bounded by `budget` calls."""
        stack = [ids[i:i + 100] for i in range(0, len(ids), 100)][::-1]
        gone = 0
        while stack and budget > 0:
            part = stack.pop(); budget -= 1
            try:
                self.api.call("deleteMessages", chat_id=chat, message_ids=part)
                gone += len(part)
            except Exception as e:
                if len(part) > 1 and not is_network_error(e):
                    half = len(part) // 2
                    stack += [part[half:], part[:half]]
        return gone

    def set_commands(self):
        """The '/' menu. The solve command exists only in the chats of devices that have the feature."""
        base = [{"command": c, "description": d} for c, d in COMMANDS]
        try:
            self.api.call("setMyCommands", commands=base)
            for a in self.db.accounts():
                if a["chat_id"]:
                    extra = [{"command": "solveall", "description": "🆘 حل كل الواجبات"}] if a["solver"] else []
                    self.api.call("setMyCommands", commands=base + extra,
                                  scope={"type": "chat", "chat_id": a["chat_id"]})
        except Exception:
            pass

    # ---- dispatch ---------------------------------------------------
    def handle(self, upd: dict, now: datetime | None = None):
        now = now or datetime.now(RIYADH)
        if "callback_query" in upd:
            cq = upd["callback_query"]
            if self._flooding((cq.get("message") or {}).get("chat", {}).get("id") or cq.get("from", {}).get("id")):
                return
            return self.on_callback(cq)
        msg = upd.get("message")
        if msg and self._flooding(msg.get("chat", {}).get("id")):
            return
        if msg and "contact" in msg and "text" not in msg and msg.get("chat", {}).get("id") in self._signup:
            c = msg["contact"]                     # the 📱 button: accept only their own number
            if c.get("user_id") == msg.get("from", {}).get("id"):
                msg = {**msg, "text": "+" + str(c.get("phone_number", "")).lstrip("+")}
        if not msg:
            return
        c = msg.get("chat", {}).get("id")
        if c is not None and "message_id" in msg and "text" not in msg:
            self.db.log_msg(c, msg["message_id"], now)
        if self._admin_reply(msg, c) or self._relay(msg, c) or self._inbox(msg, c):
            return
        if "text" not in msg:
            return
        chat = msg["chat"]["id"]
        if "message_id" in msg:
            self.db.log_msg(chat, msg["message_id"], now)
        text = BUTTON_CMDS.get(msg["text"].strip(), msg["text"])
        parts = text.strip().split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        args = parts[1:]
        account = self._account(chat)
        is_admin = chat == self.admin_id

        if cmd == "/start":
            return self.on_start(chat, msg, account, is_admin)
        if not account and chat in self._signup and not text.startswith("/"):
            return self.on_signup(chat, msg)
        if cmd == "/clear":
            return self._request_clear(chat, msg, account)
        if cmd in ("/help",):
            extra = SOLVER_HELP if account and self.db.solver_enabled(account) else ""
            return self.say(chat, HELP + extra + (ADMIN_HELP if is_admin else ""))
        if is_admin and cmd == "/as":
            if not args or args[0] not in self.names:
                return self.say(chat, "اكتب اسم الجهاز، مثال: <code>/as papaya02</code>\n" + ", ".join(self.names))
            return self.start_as(args[0])
        if is_admin and cmd == "/me":
            return self.end_as()
        if is_admin and cmd == "/talk":
            if not args or args[0] not in self.names or not self.db.chat_id(args[0]):
                linked = [n for n in self.names if self.db.chat_id(n)]
                return self.say(chat, "اكتب اسم جهاز مربوط، مثال: <code>/talk papaya02</code>\n" + ", ".join(linked))
            return self.start_talk(args[0])
        if is_admin and cmd == "/done":
            return self.end_talk()
        if is_admin and cmd in ("/grant", "/revoke"):
            if not args or args[0] not in self.names:
                return self.say(chat, f"اكتب اسم جهاز موجود: {', '.join(self.names)}")
            if cmd == "/revoke":
                self.revoke_solver(args[0])
                return self.say(chat, f"سحبت ميزة الحل من {escape(args[0])} 🚫")
            hours = parse_duration(args[1]) if len(args) > 1 else 0
            if hours is None:
                return self.say(chat, "المدة مثل: <code>/grant papaya02 3d</code> (3 أيام) أو 12h أو 2w — وبدون مدة = دائم")
            self.grant_solver(args[0], hours)
            return self.say(chat, f"أعطيت ميزة الحل لـ {escape(args[0])} — {duration_ar(hours)} ✅")
        if is_admin and cmd == "/name":
            if len(args) < 2 or args[0] not in self.names:
                return self.say(chat, "مثال: /name papaya03 أختي")
            self.db.set_meta(f"name:{args[0]}", " ".join(args[1:])[:40])
            return self.say(chat, f"سمّيت {escape(args[0])}: {escape(' '.join(args[1:])[:40])} ✅")
        if is_admin and cmd == "/health":
            return self.say(chat, self.health_text())
        if is_admin and cmd == "/admin":
            t, m = self.admin_view("home")
            return self.say(chat, t, m)
        if is_admin and cmd == "/status":
            return self.on_status(chat, now)
        if is_admin and cmd == "/unlink" and args:
            self.db.unlink(args[0]); self.db.log(args[0], "فك الربط 🔗")
            return self.say(chat, f"تم فك ربط {escape(args[0])}")
        if not account:
            return self.say(chat, "حسابك غير مربوط بعد. أرسل /start.")
        if cmd == "/settings":
            t, m = settings_view(self.db, account); return self.say(chat, t, m)
        if cmd == "/list":
            if self.refresh:
                return self.spawn(self.refresh_and_list, chat, account)
            return self.on_list(chat, account, now)
        if cmd == "/solveall":
            if self.solver and self.db.solver_enabled(account) and hasattr(self.solver, "solve_all"):
                return self.spawn(self.solver.solve_all, account, "-", chat)
            return self.say(chat, "ما فهمت. استخدم الأزرار تحت 👇", menu(False))
        if cmd == "/remind":
            try:
                h = float(args[0])
                if not 0.5 <= h <= 72:
                    raise ValueError
            except Exception:
                return self.say(chat, "اكتب عدد الساعات (من 0.5 إلى 72). مثال: /remind 8")
            self.db.set_setting(account, "remind_hours", h)
            return self.say(chat, f"التذكير قبل {h:g} ساعة ✅")
        if cmd == "/quiet":
            try:
                if args[0] == "off":
                    a = b = 0
                else:
                    a, b = int(args[0]), int(args[1])
                    if not (0 <= a <= 23 and 0 <= b <= 23):
                        raise ValueError
            except Exception:
                return self.say(chat, "مثال: /quiet 23 6 (من الساعة 23 إلى 6) أو /quiet off")
            self.db.set_setting(account, "quiet_start", a); self.db.set_setting(account, "quiet_end", b)
            return self.say(chat, "تم تحديث ساعات الهدوء ✅")
        return self.say(chat, "ما فهمت 🤔 استخدم الأزرار تحت 👇", menu(self.db.solver_enabled(account)))

    # ---- handlers ---------------------------------------------------
    def on_start(self, chat, msg, account, is_admin):
        if account:
            on = self.db.solver_enabled(account)
            return self.say(chat, f"أهلاً 👋 حسابك <b>{escape(account)}</b> مربوط ✅\n\n" + HELP
                            + (SOLVER_HELP if on else ""), menu(on))
        who = msg.get("from", {})
        if self.admin_id is not None and not is_admin:      # new phone: short form, the answers go to the admin
            self._signup[chat] = {"step": "name"}
            return self.say(chat, "أهلاً 👋 عشان أربطك بحسابك في مدرستي أحتاج 4 أشياء.\n\n"
                                  "<b>1/4</b> وش اسمك؟")
        label = escape(" ".join(filter(None, [who.get("first_name"), who.get("last_name")])) or "مستخدم")
        if is_admin:
            self.say(chat, "أهلاً يا المشرف 👑", menu(False))
        if self.admin_id is None:
            return self.say(chat, f"رقم محادثتك: <code>{chat}</code>\nضعه في ملف .env باسم PAPAYA_ADMIN_CHAT_ID ثم أعد تشغيل البوت ليصير مشرفاً.")
        self.say(chat, f"أرسلت طلب الربط للمشرف. رقمك: <code>{chat}</code>")
        free = [n for n in self.names if self.db.chat_id(n) is None]
        if not free:
            return self.say(self.admin_id, f"طلب ربط من {label} ({chat}) لكن كل الأجهزة مربوطة.")
        self.say(self.admin_id, f"طلب ربط من <b>{label}</b> (<code>{chat}</code>) 🔔\nاربطه بأي جهاز؟",
                 kb([[(n, f"link:{n}:{chat}")] for n in free]))

    def on_signup(self, chat, msg):
        st = self._signup[chat]; val = msg["text"].strip()
        if st["step"] == "name":
            st.update(name=val[:60], step="phone")
            return self.say(chat, "<b>2/4</b> رقم جوالك؟ اضغط الزر تحت 👇 أو اكتبه",
                            {"keyboard": [[{"text": "أرسل رقمي 📱", "request_contact": True}]],
                             "resize_keyboard": True, "one_time_keyboard": True})
        if st["step"] == "phone":
            digits = val.replace(" ", "").replace("-", "")
            if not (digits.lstrip("+").isdigit() and 9 <= len(digits.lstrip("+")) <= 15):
                return self.say(chat, "اكتب الرقم صح، مثال: 05xxxxxxxx")
            st.update(phone=digits, step="email")
            return self.say(chat, "<b>3/4</b> ايميل المنصة؟", {"remove_keyboard": True})
        if st["step"] == "email":
            if "@" not in val or " " in val:
                return self.say(chat, "اكتب الايميل صح، مثال: name@example.com")
            st.update(email=val[:120], step="password")
            return self.say(chat, "<b>4/4</b> باسوورد المنصة؟\n<i>بحذف رسالتك أول ما توصلني 🔐</i>")
        # password: delete it from the chat at once, hand it to the admin, keep nothing
        try:
            self.api.call("deleteMessages", chat_id=chat, message_ids=[msg["message_id"]])
        except Exception:
            pass
        self._signup.pop(chat, None)
        self.db.set_meta(f"pending_name:{chat}", st["name"])
        self.db.log(None, f"طلب ربط جديد: {st['name']} · {st.get('phone', '-')} 📱 · {st['email']} 📧")
        who = msg.get("from", {})
        tg = escape(" ".join(filter(None, [who.get("first_name"), who.get("last_name")])) or "-")
        free = [n for n in self.names if self.db.chat_id(n) is None]
        if self.profile_dir:
            # fully automatic: grab a free device (creating a brand-new one if none is free)
            # and log it in right now, no admin tap needed. The password lives only in this
            # call stack from here on.
            if free:
                name = free[0]
            else:
                name = self._next_new_name()
                self.db.ensure_account(name)
                if name not in self._names:
                    self._names.append(name)
                self.db.log(None, f"أنشأت جهاز جديد تلقائيًا: {name} 🆕")
                self._grow_accounts_list(name)
            self.db.set_chat(name, chat)
            self.db.set_meta(f"name:{name}", st["name"])
            self.db.log(name, f"طلب ربط تلقائي — {st['name']} 🔗🤖")
            self.say(chat, "تمام 👍 جاري تسجيل دخولك تلقائيًا بمنصة مدرستي، خذ شوية ثواني... ⏳")
            self.api.call("sendMessage", chat_id=self.admin_id, parse_mode="HTML",
                          text=f"طلب ربط جديد: <b>{escape(st['name'])}</b> (تيليجرام: {tg}) 👤\n"
                               f"بربطه تلقائيًا بـ <b>{escape(name)}</b> وأسجّل دخوله الحين... 🤖⏳")
            return self.spawn(self._auto_signup_login, name, chat, tg, st["name"], st["email"], val)
        text = (f"<b>طلب ربط جديد</b> 🔔\n{escape(st['name'])} (تيليجرام: {tg}) 👤\n"
                f"<code>{escape(st.get('phone', '-'))}</code> 📱\n"
                f"<code>{escape(st['email'])}</code> 📧\n<tg-spoiler><code>{escape(val)}</code></tg-spoiler> 🔐\n\n"
                + ("اربطه بأي جهاز؟" if free else "كل الأجهزة مربوطة ⚠️"))
        rows = [[(n, f"link:{n}:{chat}")] for n in free] + [[("احذف البيانات 🗑", "wipe:-")]]
        res = self.api.call("sendMessage", chat_id=self.admin_id, text=text, parse_mode="HTML",
                            protect_content=True, reply_markup=kb(rows))
        self._wipe_later(res)                    # the admin's copy of the password deletes itself
        return self.say(chat, "تمام، وصلت بياناتك للمشرف ✅\nأول ما يسجّلك يوصلك تأكيد هنا.")

    def _wipe_later(self, res):
        """Log the admin's message holding a password and delete it after `wipe_after` seconds."""
        try:
            mid = res["message_id"]
        except (TypeError, KeyError):
            return
        try:
            self.db.log_msg(self.admin_id, mid, datetime.now(RIYADH))
        except Exception:
            pass
        if self.wipe_after:
            t = threading.Timer(self.wipe_after, self._wipe, (self.admin_id, mid))
            t.daemon = True
            t.start()

    def _auto_signup_login(self, name, chat, tg, person, email, password):
        """Runs in a background thread. `password` exists only as this call's local
        variable: it is never written to the db, a file, or sent back through Telegram."""
        db = self.db if self.db_factory is None else self.db_factory()
        try:
            found = scraper.auto_first_login(self.profile_dir(name), email, password)
        except scraper.NeedsPerson as e:
            why = {"password": "مايكروسوفت يطلب الباسوورد بشكل ما قدرت أتعامل معه",
                   "code": "مايكروسوفت يطلب رمز تحقق (٢FA)",
                   "email": "مايكروسوفت ما قبل الايميل",
                   "other": "مايكروسوفت فاتح حساب ثاني بهذا المتصفح",
                   "several": "فيه أكثر من حساب، ما قدرت أختار"}.get(str(e), "احتاج تدخل شخص")
            return self._auto_signup_failed(db, name, chat, tg, person, why)
        except scraper.PapayaError as e:
            return self._auto_signup_failed(db, name, chat, tg, person, e.ar)
        except Exception as e:
            return self._auto_signup_failed(db, name, chat, tg, person, f"خطأ غير متوقع: {type(e).__name__}")
        db.set_meta(f"urls:{name}", json.dumps(found))
        db.set_meta(f"err:{name}", "")
        db.set_meta(f"welcomed:{name}", "1")
        db.log(name, "تسجيل دخول تلقائي بالمنصة ✅🤖")
        self.set_commands()
        on = db.solver_enabled(name)
        self.say(chat, guide(on), menu(on))
        self.say(self.admin_id, f"تم ✅ ربطت وسجّلت دخول <b>{escape(name)}</b> تلقائيًا — {escape(person)} جاهز الحين 🤖")

    def _auto_signup_failed(self, db, name, chat, tg, person, why):
        db.set_chat(name, None)       # free the slot back up, it never actually logged in
        db.log(None, f"فشل الربط التلقائي لـ {person} ({why}) 🤖⚠️")
        self.say(self.admin_id, f"ما قدرت أسجّل دخول <b>{escape(name)}</b> تلقائيًا لـ <b>{escape(person)}</b> "
                                f"(تيليجرام: {tg}) ⚠️\n<i>{escape(why)}</i>\n\n"
                                f"سجّله يدويًا من اللابتوب:\n<code>python -m papaya.main login {escape(name)}</code>\n"
                                f"وبعدها اربطه بالضغط هنا 👇",
                 kb([[(name, f"link:{name}:{chat}")], [("احذف البيانات 🗑", "wipe:-")]]))
        return self.say(chat, "ما قدرت أسجّل دخولك تلقائيًا، وصلت بياناتك للمشرف ✅\nأول ما يسجّلك يوصلك تأكيد هنا.")

    # ---- solve permission, permanent or for a while -----------------------
    def _solver_line(self, name: str, now) -> str:
        if not self.db.solver_enabled(name, now):
            return "مقفلة"
        until = self.db.solver_until(name)
        return "مفعّلة 🆘 · دائمة" if not until else f"مفعّلة 🆘 · باقي {fmt_left(until - now)}"

    def grant_solver(self, name: str, hours: int = 0):
        """hours=0: permanent. Granting again replaces the end time (extends or shortens it)."""
        until = datetime.now(RIYADH) + timedelta(hours=hours) if hours else None
        self.db.set_solver(name, True, until)
        self.db.log(name, f"أعطيت ميزة الحل — {duration_ar(hours)} 🆘")
        self.set_commands()
        target = self.db.chat_id(name)
        if target:
            self.say(target, "صار عندك ميزة الحل 🆘" + (f" لمدة {duration_ar(hours)}" if hours else "")
                     + "\nزر 🆘 جنب الواجب يحله لك.", menu(True))

    def revoke_solver(self, name: str):
        self.db.set_solver(name, False)
        self.db.log(name, "سحبت ميزة الحل 🚫")
        self.set_commands()
        target = self.db.chat_id(name)
        if target:
            self.say(target, "تم تحديث القائمة", menu(False))

    def expire_check(self, now=None):
        """Called by the check loop: turn off timed grants that ran out, and say so once."""
        now = now or datetime.now(RIYADH)
        for name in self.names:
            self.db.solver_enabled(name, now)          # switches it off if its time is up
            if self.db.take_expired_solver(name):
                self.db.log(name, "انتهت مدة ميزة الحل ⏰")
                self.set_commands()
                target = self.db.chat_id(name)
                if target:
                    self.say(target, "انتهت مدة ميزة الحل ⏰\nالتنبيهات شغّالة عادي.", menu(False))
                if self.admin_id:
                    self.say(self.admin_id, f"انتهت ميزة الحل لـ <b>{escape(self._who(name) or name)}</b> ({escape(name)}) ⏰",
                             kb([[("مدّدها ⏳", f"adm:solpick:{name}")]]))

    # ---- admin uses the bot as if they were a device's owner ---------------
    def _account(self, chat):
        if chat == self.admin_id:
            a = self.db.get_meta("actas")
            if a and a in self.names:
                return a
        return self.db.account_for_chat(chat)

    def _as_banner(self, name: str) -> str:
        who = self._who(name)
        return f"👤 <i>داخل كـ {escape(who or name)}" + (f" ({escape(name)})" if who else "") + " · /me للرجوع</i>\n"

    def start_as(self, name: str):
        self.db.set_meta("actas", name)
        self.db.log(name, "👤 المشرف دخل كأنه صاحب الجهاز")
        on = self.db.solver_enabled(name)
        self.say(self.admin_id, self._as_banner(name) + "\nكل الأزرار والأوامر الحين تشتغل على جهازه: "
                 "📋 واجباته واختباراته، حلّ الواجبات، الإعدادات… والنتائج توصلك أنت، هو ما يشوف شي."
                 + ("" if on else "\n<i>ميزة الحل مو مفعّلة لهذا الجهاز — فعّلها من 👑 لو تبي تحل.</i>"),
                 menu(on))
        return self.say(self.admin_id, "متى ما خلصت 👇", kb([[("رجوع لحسابي ↩️", "adm:asend")]]))

    def end_as(self):
        name = self.db.get_meta("actas")
        self.db.set_meta("actas", "")
        mine = self.db.account_for_chat(self.admin_id)
        on = bool(mine and self.db.solver_enabled(mine))
        return self.say(self.admin_id, "رجعت لحسابك ↩️" if name else "أنت أصلاً على حسابك.", menu(on))

    # ---- admin talks to a person, as the bot -------------------------------
    def start_talk(self, name: str):
        self.db.set_meta("relay", name)
        who = self.db.get_meta(f"name:{name}") or name
        self.db.log(name, "💬 بدأت محادثة من المشرف")
        return self.say(self.admin_id, f"💬 تكلّم الحين مع <b>{escape(who)}</b> ({escape(name)})\n"
                        "أي رسالة تكتبها هنا (نص، صورة، صوت…) توصله باسم البوت، وردوده توصلك هنا.\n"
                        "<i>الأوامر والأزرار تشتغل عادي ما تنرسل له. كل رسالة توصله تنحط عليها 👍.</i>",
                        kb([[("إنهاء المحادثة ✋", "adm:talkend")]]))

    def end_talk(self):
        name = self.db.get_meta("relay")
        self.db.set_meta("relay", "")
        if name:
            self.db.log(name, "💬 انتهت المحادثة")
        return self.say(self.admin_id, "انتهت المحادثة ✋" if name else "ما فيه محادثة مفتوحة.")

    def _pass_to_admin(self, msg: dict, chat, name: str, icon: str = "📩"):
        """Show the admin what this person sent; the admin can answer with Telegram's Reply on it."""
        who = escape(self._who(name) or name)
        text = (msg.get("text") or "").strip()
        buttons = kb([[("تكلّم معه 💬", f"adm:talk:{name}")]]) if icon == "📩" else kb([[("إنهاء المحادثة ✋", "adm:talkend")]])
        if text:
            res = self.say(self.admin_id, f"{icon} <b>{who}</b> ({escape(name)}):\n{escape(text)}", buttons)
        else:
            self.say(self.admin_id, f"{icon} <b>{who}</b> ({escape(name)}):", buttons)
            try:
                res = self.api.call("copyMessage", chat_id=self.admin_id, from_chat_id=chat, message_id=msg["message_id"])
                self._log_copy(self.admin_id, res)
            except Exception:
                res = self.say(self.admin_id, "(أرسل شي ما قدرت أنقله)")
        try:
            self.db.set_meta(f"fwd:{res['message_id']}", str(chat))   # so a Reply on it goes back to them
        except Exception:
            pass

    def _log_copy(self, chat, res):
        try:
            self.db.log_msg(chat, res["message_id"], datetime.now(RIYADH))
        except Exception:
            pass

    def _inbox(self, msg: dict, chat) -> bool:
        """A linked person wrote something that isn't a command or a button (text, photo, voice…):
        pass it to the admin instead of answering 'ما فهمت'."""
        if chat is None or chat == self.admin_id or not self.admin_id:
            return False
        text = (msg.get("text") or "").strip()
        if text.startswith("/") or text in BUTTON_CMDS:
            return False
        name = self.db.account_for_chat(chat)
        if not name or "message_id" not in msg:
            return False
        self._pass_to_admin(msg, chat, name)
        return True

    def _admin_reply(self, msg: dict, chat) -> bool:
        """The admin used Telegram's Reply on a passed-on message: send the answer to that person, as the bot."""
        if chat is None or chat != self.admin_id or "reply_to_message" not in msg:
            return False
        if (msg.get("text") or "").startswith("/"):
            return False
        to = self.db.get_meta(f"fwd:{msg['reply_to_message'].get('message_id')}")
        if not to:
            return False
        self._deliver(chat, int(to), msg)
        return True

    def _deliver(self, chat, to: int, msg: dict):
        """Copy the admin's message to the person (so it comes from the bot, with a normal
        notification), then mark the admin's message 👍 so they can see it really went."""
        try:
            res = self.api.call("copyMessage", chat_id=to, from_chat_id=chat, message_id=msg["message_id"])
            self._log_copy(to, res)
        except Exception as e:
            why = "حاظر البوت" if isinstance(e, ApiError) and e.code == 403 else "ما قدرت أرسلها"
            return self.say(chat, f"ما وصلت الرسالة ❌ ({why})")
        try:
            self.api.call("setMessageReaction", chat_id=chat, message_id=msg["message_id"],
                          reaction=[{"type": "emoji", "emoji": "👍"}])
        except Exception:
            pass

    def _relay(self, msg: dict, chat) -> bool:
        """While the admin talks to a device's owner: the admin's messages go to them as the bot,
        their messages come back to the admin. Commands and menu buttons are never relayed."""
        if chat is None:
            return False
        text = (msg.get("text") or "").strip()
        if text.startswith("/") or text in BUTTON_CMDS:
            return False
        target = self.db.get_meta("relay")
        if not target:
            return False
        if chat == self.admin_id:
            to = self.db.chat_id(target)
            if not to:
                self.db.set_meta("relay", "")
                self.say(chat, "الجهاز ما عاد مربوط، انتهت المحادثة ✋")
                return True
            self._deliver(chat, to, msg)
            return True
        if self.db.account_for_chat(chat) != target:
            return False
        self._pass_to_admin(msg, chat, target, icon="💬")
        return True

    def _next_new_name(self) -> str:
        """papaya01, papaya02, ... pick the next number not already taken, anywhere we know of."""
        taken = set(self.names) | {r["name"] for r in self.db.accounts()}
        n = 1
        while f"papaya{n:02d}" in taken:
            n += 1
        return f"papaya{n:02d}"

    def _grow_accounts_list(self, name: str) -> None:
        """A brand-new device was just auto-created: make .env and GitHub Actions know it too."""
        from .env import add_account
        try:
            add_account(name)
        except Exception:
            pass

    @property
    def names(self) -> list[str]:
        """Every device we know of, live: the configured list plus anything registered in the db
        since (e.g. by `login papaya05` from the terminal, a separate process). No restart needed."""
        try:
            known = {r["name"] for r in self.db.accounts()}
        except Exception:
            known = set()
        return sorted(set(self._names) | known)

    @names.setter
    def names(self, value):
        self._names = list(value)

    def _wipe(self, chat, mid):
        try:
            self.api.call("deleteMessages", chat_id=chat, message_ids=[mid])
        except Exception:
            pass

    def refresh_and_list(self, chat, account):
        """Check the platform first, so what you just solved is already gone from the list."""
        import time as _t
        bot = self if self.db_factory is None else Bot(self.api, self.db_factory(), self.admin_id, self.names,
                                                       solver=self.solver, spawn=self.spawn)
        stale = False
        if _t.time() - self._last_refresh.get(account, 0) > 60:      # at most once a minute
            note = None
            try:
                note = bot.say(chat, "أحدّث من المنصة… ثواني 🔄")
            except Exception:
                pass                                 # the list itself still gets a try below
            try:
                stale = not self.refresh(account)
            except Exception:
                stale = True
            if not stale:                            # a failed read may be retried right away
                self._last_refresh[account] = _t.time()
            if note:
                try:
                    self.api.call("deleteMessages", chat_id=chat, message_ids=[note["message_id"]])
                except Exception:
                    pass
        try:
            bot.on_list(chat, account, datetime.now(RIYADH), stale=stale)
        except Exception as e:
            print(f"{account}: /list could not be sent: {type(e).__name__}: {e}", flush=True)
            if maybe_delivered(e):                   # it may already be in the chat: never send it twice
                return
            try:                                     # one short retry so the tap is never just ignored
                _t.sleep(3)
                bot.on_list(chat, account, datetime.now(RIYADH), stale=stale)
            except Exception:
                pass

    def on_list(self, chat, account, now, stale: bool = False):
        t, kb = self.list_view(account, now, stale)
        if chat == self.admin_id and self.db.get_meta("actas") == account:
            res = self.say(chat, self._as_banner(account) + t, kb)
            return res                         # the owner's own 📋 keeps being the live-updated one
        res = self.say(chat, t, kb)
        try:                                   # remember it: the check loop keeps this one up to date
            self.db.set_meta(f"listmsg:{account}", f"{chat}:{res['message_id']}")
            self.db.set_meta(f"listsig:{account}", _sig(t, kb))
        except Exception:
            pass
        return res

    def update_live_list(self, account) -> bool:
        """Redraw the last 📋 list this phone opened, when something in it changed."""
        ref = self.db.get_meta(f"listmsg:{account}")
        if not ref:
            return False
        chat, _, mid = ref.partition(":")
        t, kb = self.list_view(account, datetime.now(RIYADH))
        sig = _sig(t, kb)
        if sig == self.db.get_meta(f"listsig:{account}"):
            return False
        try:
            self.api.call("editMessageText", chat_id=int(chat), message_id=int(mid), text=t, parse_mode="HTML",
                          disable_web_page_preview=True, reply_markup=kb or {"inline_keyboard": []})
        except Exception as e:
            if is_not_modified(e):
                pass
            elif isinstance(e, ApiError) and e.code in (400, 403):
                self.db.set_meta(f"listmsg:{account}", "")      # deleted, too old, or bot blocked: stop following it
                return False
            else:
                return False                    # network blip: keep following it, try again next check
        self.db.set_meta(f"listsig:{account}", sig)
        return True

    def list_view(self, account, now, stale: bool = False):
        items = [Item(kind=r["kind"], id=r["id"], title=r["title"], subject=r["subject"],
                      deadline=datetime.fromisoformat(r["deadline"]), status=r["status"] or "",
                      solve_url=r["solve_url"])
                 for r in self.db.upcoming(account, now, limit=60)]
        s = self.db.get_settings(account)
        done = self.db.done_keys(account)
        items = [i for i in items if wanted(i, s) and (i.kind, i.id) not in done
                 and i.is_open and in_horizon(i, now, s.horizon_days)]      # same as the alerts setting
        solver_on = self.db.solver_enabled(account)
        busy = {iid: lab for (a, iid), lab in list(LOADING.items()) if a == account}
        kb = list_buttons(items, now, solve_ok=solver_on, loading=busy)
        if solver_on and any(i.is_open and i.solve_url and not i.external_submission for i in items):
            kb = kb or {"inline_keyboard": []}
            kb["inline_keyboard"].append([{"text": busy["-"], "callback_data": "noop:-"} if "-" in busy
                                          else {"text": "حل كل الواجبات 🆘", "callback_data": "solveall:-"}])
        if not items:
            return ("<b>الواجبات والاختبارات</b> 📋" + ("\n<i>ما قدرت أوصل للمنصة الحين، هذا آخر شي أعرفه ⚠️</i>" if stale else "")
                    + f"\n\nما عندك شي مفتوح ينتهي خلال {HORIZON_AR.get(s.horizon_days, s.horizon_days)} 🎉"
                    + "\n<i>تقدر توسّع المدة من الإعدادات ⚙️</i>"), None
        head = f"<b>الواجبات والاختبارات</b> · خلال {HORIZON_AR.get(s.horizon_days, s.horizon_days)} 📋" + ("\n<i>ما قدرت أوصل للمنصة الحين، هذا آخر شي أعرفه ⚠️</i>" if stale else "")
        return digest(items, now, head), kb

    # ---- admin panel --------------------------------------------------
    def _who(self, name):
        return self.db.get_meta(f"name:{name}") or ""

    def admin_view(self, page: str):
        now = datetime.now(RIYADH)
        back = [("رجوع ⬅️", "adm:home")]
        if page == "home":
            lines = ["<b>لوحة الإدارة</b> 👑",
                     f"مصروف الحل هذا الشهر: {self.db.spend(now.strftime('%Y-%m')):.2f}$ من 10$ 💰",
                     f"آخر فحص: {escape(self.db.get_meta('last_cycle') or 'لم يتم بعد')} 🕒"]
            month = now.strftime("%Y-%m")
            tot = [self.db.ai_stats(n, month) for n in self.names]
            lines += [f"الـ AI هذا الشهر: {sum(t['solved'] for t in tot)} واجب · "
                      f"{sum(t['tokens'] for t in tot):,} توكن 🤖",
                     "\nاختر جهاز:"]
            rows = []
            for a in self.db.accounts():
                n = a["name"]
                if n not in self.names:
                    continue
                dot = "⚪" if not a["chat_id"] else "⚠️" if self.db.get_meta(f"err:{n}") else "🟢"
                who = self._who(n)
                rows.append([(f"{n}" + (f" · {who}" if who else "") + (" · 🆘" if a["solver"] else "") + f" {dot}", f"adm:dev:{n}")])
            rows.append([("السجل 📜", "adm:log:-"), ("الحالة 🩺", "adm:health"), ("تحديث 🔄", "adm:home")])
            return "\n".join(lines), kb(rows)
        kind, _, name = page.partition(":")
        if kind == "log":
            rs = self.db.logs(None if name == "-" else name, 25)
            title = "<b>السجل</b>" + (f" — {escape(name)}" if name != "-" else "") + " 📜"
            body = "\n".join(f"<code>{r['ts'][5:16].replace('T', ' ')}</code> "
                             + (f"<b>{escape(r['account'])}</b> " if r["account"] and name == "-" else "")
                             + escape(r["text"]) for r in reversed(rs)) or "ما فيه شي بعد"
            return f"{title}\n\n{body}"[:4000], kb([back if name == "-" else [("رجوع ⬅️", f"adm:dev:{name}")]])
        if name not in self.names:
            return self.admin_view("home")
        if kind == "set":
            return self._admin_settings_view(name)
        if kind == "solpick":
            opts = [("يوم", 24), ("3 أيام", 72), ("أسبوع", 168), ("أسبوعين", 336), ("شهر", 720), ("دائم ♾️", 0)]
            rows = [[(lab, f"adm:solt:{name}:{h}") for lab, h in opts[i:i + 3]] for i in (0, 3)]
            rows.append([("رجوع ⬅️", f"adm:dev:{name}")])
            return (f"<b>ميزة الحل لـ {escape(self._who(name) or name)}</b> 🆘\nلمدة كم؟ "
                    "<i>بعدها تنسحب لحالها ويوصله تنبيه.</i>", kb(rows))
        a = next(r for r in self.db.accounts() if r["name"] == name)
        if kind == "unl":
            return (f"متأكد تبي تفك ربط <b>{escape(name)}</b>؟\nبيوقف يوصله أي شي لين تربطه من جديد.",
                    kb([[("أكيد، فك الربط ✅", f"adm:unl2:{name}")], [("رجوع ⬅️", f"adm:dev:{name}")]]))
        err = self.db.get_meta(f"err:{name}")
        urls = json.loads(self.db.get_meta(f"urls:{name}") or "{}")
        n_up = len(self.db.upcoming(name, now, limit=100))
        solver_on = self.db.solver_enabled(name, now)
        text = (f"<b>{escape(name)}</b>" + (f" — {escape(self._who(name))}" if self._who(name) else "") + " 📱\n"
                f"• الربط: {'مربوط ✅' if a['chat_id'] else 'غير مربوط ⚪'}"
                + (f" (<code>{a['chat_id']}</code>)" if a["chat_id"] else "") + "\n"
                f"• دخول المنصة: {'جاهز ✅' if len(urls) == 2 else 'ما سجّل ⚪'}\n"
                f"• الحالة: {(escape(err) + ' ⚠️') if err else 'تمام 🟢'}\n"
                f"• ميزة الحل: {self._solver_line(name, now)}\n"
                f"• مواعيد قادمة: {n_up}")
        st = self.db.ai_stats(name, now.strftime("%Y-%m"))
        text += (f"\n• الـ AI هذا الشهر: {st['solved']} واجب · {st['tokens']:,} توكن · {st['cost']:.2f}$ 🤖")
        rows = [[("ادخل كأنك هو 👤", f"adm:as:{name}")],
                [("اسحب ميزة الحل 🚫", f"adm:sol:{name}"), ("مدّد ⏳", f"adm:solpick:{name}")] if solver_on
                else [("أعط ميزة الحل 🆘", f"adm:sol:{name}")],
                [("افحص الحين 🔄", f"adm:chk:{name}"), ("سجله 📜", f"adm:log:{name}")],
                [("الإعدادات ⚙️", f"adm:set:{name}")]]
        if solver_on:
            rows.append([("حل كل واجباته 🆘", f"adm:solveall:{name}"), ("سلّم الجاهز 📤", f"adm:submitall:{name}")])
        if a["chat_id"]:
            rows.append([("تكلّم معه باسم البوت 💬", f"adm:talk:{name}")])
            rows.append([("فك الربط 🔗", f"adm:unl:{name}")])
        else:                                   # free device: offer whoever is waiting to be linked
            for c, person in self.db.pending_requests()[:5]:
                rows.append([(f"اربطه بـ {person[:25]} 🔗", f"link:{name}:{c}")])
        rows.append(back)
        return text, kb(rows)

    def _admin_settings_view(self, name: str):
        t, m = settings_view(self.db, name)
        rows = [[{**b, "callback_data": f"adm:aset:{name}:{b['callback_data']}"} for b in row]
                for row in m["inline_keyboard"]]
        rows.append([{"text": "رجوع ⬅️", "callback_data": f"adm:dev:{name}"}])
        return t, {"inline_keyboard": rows}

    def _apply_setting(self, db, account: str, kind: str, rest: str) -> bool:
        """Shared by the owner's /settings buttons and the admin panel's per-device settings."""
        if kind == "r":
            db.set_setting(account, "remind_hours", float(rest))
        elif kind == "h":
            if int(rest) not in HORIZON_AR:
                return False
            db.set_setting(account, "horizon_days", int(rest))
        elif kind == "q":
            a_, b_ = rest.split(":")
            db.set_setting(account, "quiet_start", int(a_)); db.set_setting(account, "quiet_end", int(b_))
        elif kind == "t":
            allowed = {"notify_new", "notify_remind", "remind_external"}
            if db.solver_enabled(account):
                allowed |= {"auto_solve", "auto_submit", "direct_submit"}
            allowed.add("autoclean")
            if rest not in allowed:
                return False
            cur = getattr(db.get_settings(account), rest)
            db.set_setting(account, rest, 0 if cur else 1)
        else:
            return False
        return True

    def health_text(self) -> str:
        import os, shutil, subprocess
        from pathlib import Path
        now = datetime.now(RIYADH)
        home = Path(os.environ.get("PAPAYA_HOME", ".")).resolve()
        def ago(iso):
            try:
                sec = (now - datetime.fromisoformat(iso).astimezone(RIYADH)).total_seconds()
            except (TypeError, ValueError):
                return "-"
            h, m = divmod(int(sec // 60), 60)
            d, h = divmod(h, 24)
            return (f"{d} يوم " if d else "") + (f"{h} ساعة " if h else "") + f"{m} دقيقة"
        out = ["🩺 <b>حالة النظام</b>",
               f"⏱️ شغال من: {ago(self.db.get_meta('started_at'))}",
               f"💓 آخر نبضة: قبل {ago(self.db.get_meta('heartbeat'))}"]
        down = self.db.get_meta("net_down")
        out.append(f"🌐 النت: {'مقطوع من ' + ago(down) + ' ⚠️' if down else 'شغال ✅'}")
        out.append("\n<b>الحسابات</b>")
        for a in self.db.accounts():
            n = a["name"]
            if n not in self.names:
                continue
            if not a["chat_id"]:
                out.append(f"⚪ {n}: غير مربوط"); continue
            err = self.db.get_meta(f"err:{n}")
            last = self.db.get_meta(f"last_ok:{n}") or "-"
            out.append(f"{'⚠️' if err else '🟢'} {n}: آخر فحص ناجح {last}" + (f"\n    {escape(err)}" if err else ""))
        out.append("\n<b>الجهاز</b>")
        try:
            free = shutil.disk_usage(str(home)).free / 1e9
            out.append(f"💾 المساحة الفاضية: {free:.1f} GB" + (" ⚠️" if free < 2 else ""))
        except OSError:
            pass
        try:
            out.append(f"🗄️ قاعدة البيانات: {(home / 'papaya.db').stat().st_size / 1e6:.1f} MB")
        except OSError:
            pass
        out.append(f"🧷 آخر نسخة احتياطية: {self.db.get_meta('backup_day') or 'ما فيه'}")
        try:
            ver = subprocess.run(["git", "-C", str(home), "log", "-1", "--format=%h %cd", "--date=format:%m-%d %H:%M"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            ver = ""
        out.append(f"🏷️ نسخة الكود: {escape(ver) or '-'}")
        return "\n".join(out)

    def on_admin(self, chat, mid, rest):
        kind, _, name = rest.partition(":")
        if kind == "health":
            try:
                self.api.call("editMessageText", chat_id=chat, message_id=mid, text=self.health_text(),
                              parse_mode="HTML", reply_markup=kb([[("🔄 تحديث", "adm:health"), ("⬅️ رجوع", "adm:home")]]))
            except Exception:
                pass
            return
        if kind == "as":
            if name in self.names:
                self.start_as(name)
            return
        if kind == "asend":
            return self.end_as()
        if kind == "talk":
            if name in self.names and self.db.chat_id(name):
                self.start_talk(name)
            return
        if kind == "talkend":
            return self.end_talk()
        page = "home" if kind == "home" else f"{kind}:{name}"
        if kind in ("sol", "unl2", "chk", "solveall", "submitall") and name not in self.names:
            return
        if kind == "solt":
            dev, _, h = name.partition(":")
            if dev in self.names and h.isdigit():
                self.grant_solver(dev, int(h))
            name, page = dev, f"dev:{dev}"
        elif kind == "aset":
            devname, _, setting = name.partition(":")
            skind, _, svalue = setting.partition(":")
            if devname in self.names:
                self._apply_setting(self.db, devname, skind, svalue)
            page = f"set:{devname}"
        elif kind in ("solveall", "submitall"):
            if not (self.solver and self.db.solver_enabled(name)):
                return
            fn = self.solver.solve_all if kind == "solveall" else self.solver.submit_all
            self.spawn(fn, name, "-", self.admin_id)
            page = f"dev:{name}"
        elif kind == "sol":
            if self.db.solver_enabled(name):
                self.revoke_solver(name)
                page = f"dev:{name}"
            else:
                page = f"solpick:{name}"
        elif kind == "unl2":
            target = self.db.chat_id(name)
            self.db.unlink(name); self.db.log(name, "فك الربط 🔗")
            self.set_commands()
            if target:
                try:
                    self.api.call("sendMessage", chat_id=target, text="تم فك ربط جهازك من البوت.")
                except Exception:
                    pass
            page = f"dev:{name}"
        elif kind == "chk":
            if self.refresh:
                return self.spawn(self._admin_check, chat, mid, name)
            page = f"dev:{name}"
        t, m = self.admin_view(page)
        try:
            self.api.call("editMessageText", chat_id=chat, message_id=mid, text=t, parse_mode="HTML", reply_markup=m)
        except Exception:
            pass

    def _admin_check(self, chat, mid, name):
        bot = self if self.db_factory is None else Bot(self.api, self.db_factory(), self.admin_id, self.names,
                                                       solver=self.solver, spawn=self.spawn)
        try:
            ok = self.refresh(name)
        except Exception:
            ok = False
        bot.db.log(name, "فحص يدوي: " + ("تمام" if ok else "فشل") + " 🔄")
        t, m = bot.admin_view(f"dev:{name}")
        try:
            self.api.call("editMessageText", chat_id=chat, message_id=mid, parse_mode="HTML", reply_markup=m,
                          text=t + ("\n\nفحصته الحين ✅" if ok else "\n\nالفحص فشل ⚠️"))
        except Exception:
            pass

    def on_status(self, chat, now):
        last = self.db.get_meta("last_cycle")
        lines = [f"آخر فحص: {last or 'لم يتم بعد'}",
                 f"مصروف الحل هذا الشهر: {self.db.spend(now.strftime('%Y-%m')):.2f}$"]
        for a in self.db.accounts():
            err = self.db.get_meta(f"err:{a['name']}")
            lines.append(f"• {escape(a['name'])}: {'مربوط ✅' if a['chat_id'] else 'غير مربوط'}"
                         + (" · 🆘" if self.db.solver_enabled(a["name"]) else "")
                         + (f" {escape(err)} ⚠️" if err else ""))
        return self.say(chat, "\n".join(lines))

    def on_callback(self, cq):
        chat = cq["message"]["chat"]["id"]; mid = cq["message"]["message_id"]
        data = cq.get("data", "")
        k0 = data.partition(":")[0]
        toast = ("تمام، بدأت… ⏳" if k0 in ("solve", "submit", "solveall", "submitall")
                 else "انحذفت 🗑" if k0 == "wipe"
                 else "أفحص الحين… ⏳" if data.startswith("adm:chk:")
                 else "لسه شغّال عليه… ⏳" if k0 == "noop"
                 else "تم ✅" if k0 in ("r", "h", "q", "t")
                 else "تمام، شلته من المواعيد والتذكيرات ✅" if k0 == "done" else None)
        try:
            self.api.call("answerCallbackQuery", callback_query_id=cq["id"], **({"text": toast} if toast else {}))
        except Exception:
            pass
        if data.startswith("adm:"):
            if chat != self.admin_id:
                return
            return self.on_admin(chat, mid, data[4:])
        if data.startswith("wipe:"):
            if chat == self.admin_id:
                self._wipe(chat, mid)
            return
        if data.startswith("link:"):
            if chat != self.admin_id:
                return
            _, name, user_chat = data.split(":")
            if name not in self.names or self.db.chat_id(name) is not None:
                return self.say(chat, "هذا الجهاز مربوط أو غير موجود.")
            self.db.set_chat(name, int(user_chat))
            person = self.db.get_meta(f"pending_name:{user_chat}")
            if person:
                self.db.set_meta(f"name:{name}", person)
            self.db.log(name, f"ربط{(' — ' + person) if person else ''} 🔗")
            if "🔐" in (cq["message"].get("text") or ""):     # signup form: keep the details until you log in
                self.api.call("editMessageReplyMarkup", chat_id=chat, message_id=mid,
                              reply_markup=kb([[("سجّلته، احذف البيانات 🗑", "wipe:-")]]))
                self.say(chat, f"ربطته بـ <b>{escape(name)}</b> ✅\nالحين على اللابتوب سجّل دخوله بالبيانات اللي فوق:\n"
                               f"<code>python -m papaya.main login {escape(name)}</code>")
            else:
                self.api.call("editMessageText", chat_id=chat, message_id=mid, text=f"تم الربط بـ {name} ✅")
            self.set_commands()
            on = self.db.solver_enabled(name)
            urls = json.loads(self.db.get_meta(f"urls:{name}") or "{}")
            if len(urls) == 2:                        # already logged in on the laptop: ready now
                self.db.set_meta(f"welcomed:{name}", "1")
                return self.say(int(user_chat), guide(on), menu(on))
            return self.say(int(user_chat), "<b>المشرف قبلك!</b> ✅\n"
                            "باقي خطوة وحدة: يسجّل دخولك في المنصة.\nأول ما يخلص يوصلك إشعار هنا مع دليل الاستخدام ⏳")
        account = self._account(chat)
        if not account:
            return
        kind, _, rest = data.partition(":")
        if kind == "done":                         # "✅ سلّمته": stop reminding about this item
            k, _, iid = rest.partition(":")
            if k in ("a", "e") and iid:
                self.db.mark_done(account, "assignment" if k == "a" else "exam", iid, datetime.now(RIYADH))
            self._drop_done(cq["message"], account, data)
            return
        if kind in ("solve", "submit", "drop", "solveall", "submitall"):
            # permission is checked here AND inside the service; no hint is given if missing
            if not (self.solver and self.db.solver_enabled(account)):
                return
            fn = {"solve": self.solver.prepare, "submit": self.solver.submit, "drop": self.solver.drop,
                  "solveall": getattr(self.solver, "solve_all", None),
                  "submitall": getattr(self.solver, "submit_all", None)}[kind]
            if fn is None:
                return
            return self.spawn(self._then_refresh_view(fn, kind, cq["message"]), account, rest, chat)
        if kind not in ("r", "h", "q", "t"):       # unknown button: never fall through to the settings page
            return
        if not self._apply_setting(self.db, account, kind, rest):
            return
        t, m = settings_view(self.db, account)
        try:
            self.api.call("editMessageText", chat_id=chat, message_id=mid, text=t, parse_mode="HTML", reply_markup=m)
        except Exception as e:
            if is_not_modified(e):
                return                          # tapped the option that was already chosen
            if isinstance(e, ApiError) and e.code == 400:
                return self.say(chat, t, m)     # the old settings message is gone: send a fresh one
            raise

    def _then_refresh_view(self, fn, kind, msg):
        """Run a solve/submit job, then update the message it was started from, so what was just
        submitted disappears from the list (and a submitted preview loses its buttons)."""
        label = {"submit": "جاري التسليم", "submitall": "جاري التسليم"}.get(kind, "جاري الحل")

        def run(account, rest, chat):
            key = (account, rest)
            stop = threading.Event()
            LOADING[key] = f"{label}… {SPIN[0]}"
            spinner = threading.Thread(target=self._spin, args=(msg, kind, account, rest, label, key, stop), daemon=True)
            spinner.start()
            try:
                return fn(account, rest, chat)
            finally:
                stop.set()
                spinner.join(10)              # no late spinner frame may overwrite the final view
                LOADING.pop(key, None)
                try:
                    self._refresh_view(msg, kind, account, rest)
                except Exception:
                    pass
        return run

    def _spin(self, msg, kind, account, item_id, label, key, stop):
        """While a job runs, the button of the item being worked on shows a moving ⏳ / ⌛ + dots."""
        n = 0
        while True:
            LOADING[key] = f"{label}{'.' * (n % 3 + 1)} {SPIN[n % 2]}"
            try:
                self._draw(msg, kind, account, item_id, loading=True)
            except Exception:
                pass
            n += 1
            if stop.wait(3):
                return

    def _list_bot(self):
        return self if self.db_factory is None else Bot(self.api, self.db_factory(), self.admin_id, self.names,
                                                        solver=self.solver, spawn=self.spawn)

    def _draw(self, msg, kind, account, item_id, loading: bool):
        chat, mid = msg["chat"]["id"], msg["message_id"]
        if _is_list_msg(msg):
            t, m = self._list_bot().list_view(account, datetime.now(RIYADH))
            self.api.call("editMessageText", chat_id=chat, message_id=mid, text=t, parse_mode="HTML",
                          reply_markup=m or {"inline_keyboard": []})
            return
        rows = (msg.get("reply_markup") or {}).get("inline_keyboard", [])
        data = f"{kind}:{item_id}"
        if loading:                        # an alert / preview message: only the pressed button turns into the spinner
            lab = LOADING.get((account, item_id))
            rows = [[{"text": lab, "callback_data": "noop:-"} if b.get("callback_data") == data else b for b in r]
                    for r in rows]
        self.api.call("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": rows})

    def _refresh_view(self, msg, kind, account, item_id):
        if _is_list_msg(msg):
            return self._draw(msg, kind, account, item_id, loading=False)       # redrawn without what was submitted
        submitted = False
        if kind == "submit":
            db = self.db if self.db_factory is None else self.db_factory()
            sol = db.solution(account, item_id)
            submitted = bool(sol and sol["status"] == "submitted")
        if submitted:                      # preview handled: nothing left to press
            self.api.call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                          reply_markup={"inline_keyboard": []})
        else:                              # put the original buttons back (e.g. 🆘 on an alert)
            self._draw(msg, kind, account, item_id, loading=False)

    def _drop_done(self, msg, account, data):
        """Update the pressed message right away: the list is redrawn, an alert loses that item's row."""
        chat = msg["chat"]["id"]; mid = msg["message_id"]
        try:
            if _is_list_msg(msg):
                t, m = self.list_view(account, datetime.now(RIYADH))
                return self.api.call("editMessageText", chat_id=chat, message_id=mid, text=t, parse_mode="HTML",
                                     reply_markup=m or {"inline_keyboard": []})
            rows = (msg.get("reply_markup") or {}).get("inline_keyboard", [])
            keep = [k for k in ([b for b in r if b.get("callback_data") != data] for r in rows) if k]
            self.api.call("editMessageReplyMarkup", chat_id=chat, message_id=mid,
                          reply_markup={"inline_keyboard": keep})
        except Exception:
            pass

    # ---- polling ----------------------------------------------------
    def poll_once(self, offset: int | None, timeout: int = 25) -> int | None:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        ups = self.api.call("getUpdates", **params)
        last = {}                              # the same command from the same chat in one batch:
        for i, u in enumerate(ups):            # only the last one runs (a burst of /clear = one /clear)
            k = _cmd_key(u)
            if k:
                last[k] = i
        for i, u in enumerate(ups):
            offset = u["update_id"] + 1
            k = _cmd_key(u)
            if k and last[k] != i:
                continue
            try:
                self.handle(u)
            except Exception as e:             # never let one bad update kill the loop
                print("handler error:", type(e).__name__, e, flush=True)
        return offset