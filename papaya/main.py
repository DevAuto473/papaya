"""CLI: login <acct> | verify <acct> | check | run"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .bot import Api, Bot
from .cycle import run_cycle
from .db import DB
from .env import accounts, admin_id, load_env
from .notifier import TelegramNotifier
from .parser import RIYADH, parse_assignments, parse_exams
from .service import SolveService
from . import scraper

HOME = Path(os.environ.get("PAPAYA_HOME", ".")).resolve()


def profile_dir(acct: str) -> Path:
    return HOME / "profiles" / acct


def check_account(db: DB, acct: str, notifier, admin_notify, fetch=None, finished: bool = True) -> str:
    """One cycle for one account. Reports errors to the admin once per distinct error."""
    if db.chat_id(acct) is None:
        return f"{acct}: not linked to a phone, skipped"
    urls = json.loads(db.get_meta(f"urls:{acct}") or "{}")
    fetch = fetch or (lambda: scraper.fetch(profile_dir(acct), urls, finished=finished))
    try:
        a_html, e_html, *rest = fetch()
        if scraper.RELOGGED.pop(str(profile_dir(acct).resolve()), None):
            db.log(acct, "🔑 جدد الدخول للمنصة لحاله")
            print(f"{acct}: logged in again automatically", flush=True)
        exams = parse_exams(e_html)
        fin = scraper.finished_ids(rest[0] if rest else "")
        for x in exams:                       # solved exam still shown in 'Current' but listed in 'Completed'
            if x.id in fin and ("exam", x.id) not in db.done_keys(acct):
                db.mark_done(acct, "exam", x.id, datetime.now(RIYADH))
                db.log(acct, f"اختبار انحل (من المكتملة): {x.title} ✅")
        items = parse_assignments(a_html) + exams
        run_cycle(db, acct, items, notifier)
        if db.get_meta(f"err:{acct}"):
            admin_notify(f"{acct}: رجع يشتغل ✅")
            db.log(acct, "رجع يشتغل ✅")
            db.set_meta(f"err:{acct}", "")
        return f"{acct}: {len(items)} items"
    except scraper.PapayaError as e:
        if db.get_meta(f"err:{acct}") != e.ar:
            db.set_meta(f"err:{acct}", e.ar)
            hint = (f"\nللتجديد: python -m papaya.main login {acct}"
                    if isinstance(e, scraper.SessionExpired) or "login" in e.ar else "")
            admin_notify(f"{acct}: {e.ar} ⚠️{hint}")
            db.log(acct, f"{e.ar} ⚠️")
        return f"{acct}: ERROR - {e.en}"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        sys.exit("Put TELEGRAM_BOT_TOKEN in the .env file")
    return t


def _admin_notifier(token: str):
    n = TelegramNotifier(token)
    return lambda text: admin_id() and n.send(admin_id(), text)


def _log_sent(db_path: str):
    return lambda chat, mid: DB(db_path).log_msg(chat, mid, datetime.now(RIYADH))


def _still_open(db_path: str):
    """After a submit we could not confirm on the page: is the assignment still in 'Current'?"""
    def verify(acct: str, item_id: str):
        urls = json.loads(DB(db_path).get_meta(f"urls:{acct}") or "{}")
        try:
            a_html, *_ = scraper.fetch(profile_dir(acct), urls, finished=False)
        except scraper.PapayaError:
            return None
        for it in parse_assignments(a_html):
            if it.id == item_id:
                return not it.is_open or it.done          # still listed and open -> not submitted
        return True                                       # gone from 'Current' -> submitted
    return verify


def autoclean(db: DB, token: str, now: datetime | None = None, notifier=None) -> int:
    """Delete messages older than 24h in chats that keep the setting on. Telegram can't delete
    messages older than 48h, so the cycle (every ~30 min) has a full day to catch each one."""
    from datetime import timedelta
    now = now or datetime.now(RIYADH)
    n = notifier or TelegramNotifier(token)
    total = 0
    for a in db.accounts():
        if not a["chat_id"] or not db.get_settings(a["name"]).autoclean:
            continue
        ids = db.old_msgs(a["chat_id"], now - timedelta(hours=24))
        if ids:
            n.delete(a["chat_id"], ids)
            db.forget_msgs(a["chat_id"], ids)
            total += len(ids)
    return total


def make_solver(db_path: str, token: str) -> SolveService:
    from . import assignment_page
    assignment_page.SCREENSHOT_DIR = str(HOME / "diagnose")
    return SolveService(db_factory=lambda: DB(db_path), notifier=TelegramNotifier(token, _log_sent(db_path)),
                        admin_id_fn=admin_id, profile_dir_fn=profile_dir, verify_fn=_still_open(db_path))


def _interval(night_min: int) -> float:
    """Every ~10 min from 7am to 11pm (when people actually solve things), slower at night."""
    day_min = int(os.environ.get("PAPAYA_DAY_INTERVAL_MIN", "10"))
    h = datetime.now(RIYADH).hour
    return (day_min if 7 <= h < 23 else night_min) * 60


GAP_MIN = 15          # no sign of life for this long = the laptop slept or the bot was off


def _fmt_gap(sec: float) -> str:
    h, m = divmod(int(sec // 60), 60)
    return f"{h} ساعة و{m} دقيقة" if h and m else f"{h} ساعة" if h else f"{m} دقيقة"


def heartbeat(db: DB, anotify, now: datetime | None = None) -> bool:
    """Called about once a minute. True (and the admin is told) when the bot was off or asleep."""
    now = now or datetime.now(RIYADH)
    last = db.get_meta("heartbeat")
    db.set_meta("heartbeat", now.isoformat(timespec="seconds"))
    if not last:
        return False
    since = datetime.fromisoformat(last).astimezone(RIYADH)
    gap = (now - since).total_seconds()
    if gap < GAP_MIN * 60:
        return False
    db.log(None, f"كان طافي من {since:%m-%d %H:%M} ({_fmt_gap(gap)}) 💤")
    try:
        anotify(f"البوت كان طافي أو اللابتوب نايم من {since:%m-%d %H:%M} ({_fmt_gap(gap)}) ⚠️\n"
                "رجع يشتغل، وأفحص كل الحسابات الحين ✅")
    except Exception:
        pass
    return True


def backup(db_path: str, now: datetime | None = None, keep: int = 7):
    """Once a day: a copy of the database (settings, log, stats). Keeps the last `keep` days."""
    import sqlite3
    now = now or datetime.now(RIYADH)
    db = DB(db_path)
    day = now.strftime("%Y-%m-%d")
    if db.get_meta("backup_day") == day:
        return None
    folder = Path(os.environ.get("PAPAYA_BACKUP_DIR") or (HOME / "backups"))
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"papaya-{day}.db"
    src, dst = sqlite3.connect(db_path), sqlite3.connect(str(out))
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    for old in sorted(folder.glob("papaya-*.db"))[:-keep]:
        old.unlink()
    db.set_meta("backup_day", day)
    db.log(None, f"نسخة احتياطية: {out.name} 💾")
    return out


def cycle_loop(db_path: str, token: str, interval_min: int):
    db = DB(db_path)                       # own connection for this thread
    notifier = TelegramNotifier(token, _log_sent(db_path))
    anotify = _admin_notifier(token)
    solver = make_solver(db_path, token)
    heartbeat(db, anotify)                 # tells you if it was off since last time
    while True:
        for acct in accounts():
            try:
                out = check_account(db, acct, notifier, anotify)
            except Exception as e:     # one broken account/bug must never stop the reminder loop
                out = f"{acct}: ERROR - crashed: {type(e).__name__}: {e}"
                if db.get_meta(f"crash:{acct}") != type(e).__name__:
                    db.set_meta(f"crash:{acct}", type(e).__name__)
                    anotify(f"{acct}: خطأ في البرنامج ({type(e).__name__}) — الفحص مستمر لبقية الحسابات ⚠️")
            print(datetime.now(RIYADH).strftime("%H:%M"), out, flush=True)
            if "ERROR" not in out and "skipped" not in out:
                try:
                    for r in solver.auto(acct):
                        print(datetime.now(RIYADH).strftime("%H:%M"), f"{acct}: auto-solver -> {r}", flush=True)
                except Exception as e:     # the reminder loop must never die because of the solver
                    print(f"{acct}: auto-solver crashed: {type(e).__name__}", flush=True)
            heartbeat(db, anotify)
            time.sleep(random.uniform(5, 20))
        db.set_meta("last_cycle", datetime.now(RIYADH).strftime("%Y-%m-%d %H:%M"))
        try:
            gone = autoclean(db, token)
            if gone:
                print(datetime.now(RIYADH).strftime("%H:%M"), f"autoclean: deleted {gone} old messages", flush=True)
        except Exception as e:
            print(f"autoclean failed: {type(e).__name__}", flush=True)
        try:
            out = backup(db_path)
            if out:
                print(datetime.now(RIYADH).strftime("%H:%M"), f"backup saved: {out}", flush=True)
        except Exception as e:
            print(f"backup failed: {type(e).__name__}: {e}", flush=True)
        wait = _interval(interval_min)
        end = time.time() + wait + random.uniform(-0.1, 0.1) * wait
        while time.time() < end:           # sleep in 1-minute steps so a laptop sleep is noticed
            time.sleep(min(60, max(1, end - time.time())))
            if heartbeat(db, anotify):
                break                      # was asleep: check everything right away


def main(argv=None):
    load_env()
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "run"
    db_path = str(HOME / "papaya.db")
    if cmd == "login":
        acct = argv[1]
        if acct not in accounts():
            sys.exit(f"{acct} is not listed in PAPAYA_ACCOUNTS in .env")
        db = DB(db_path); db.ensure_account(acct)
        urls = json.loads(db.get_meta(f"urls:{acct}") or "{}")
        found = scraper.setup_manual(profile_dir(acct))
        urls.update(found)                      # fixing one page keeps the other
        db.set_meta(f"urls:{acct}", json.dumps(urls))
        if found:
            db.set_meta(f"err:{acct}", "")
            db.log(acct, "تسجيل دخول للمنصة" + ("" if len(urls) == 2 else " (ناقص صفحة)") + " 🔑")
        for k in ("assign", "exams"):
            print(f"   {k}: {scraper.mask(urls[k]) if k in urls else 'NOT SAVED'}")
        print("[ok] ready" if len(urls) == 2 else "[fail] both pages are needed - run login again")
    elif cmd == "verify":
        acct = argv[1]
        db = DB(db_path)
        urls = json.loads(db.get_meta(f"urls:{acct}") or "{}")
        try:
            a_html, e_html = scraper.fetch(profile_dir(acct), urls)
        except scraper.PapayaError as e:
            sys.exit(f"[fail] {e.en}")
        now = datetime.now(RIYADH)
        items = sorted(parse_assignments(a_html) + parse_exams(e_html), key=lambda i: i.deadline)
        live = [i for i in items if i.deadline > now]
        print(f"[ok] assignments: {sum(i.kind == 'assignment' for i in items)}, "
              f"exams: {sum(i.kind == 'exam' for i in items)}, upcoming: {len(live)}")
        for i in live[:10]:
            state = "open" if i.is_open else "not open yet"
            print(f"   {i.kind:<10} due {i.deadline:%Y-%m-%d %H:%M}  {state:<12} {i.subject}")
        print("(nothing was sent to Telegram)")
    elif cmd == "check":
        db = DB(db_path); tok = _token()
        for acct in accounts():
            print(check_account(db, acct, TelegramNotifier(tok), _admin_notifier(tok)))
    elif cmd == "run":
        tok = _token()
        interval = int(os.environ.get("PAPAYA_INTERVAL_MIN", "30"))
        threading.Thread(target=cycle_loop, args=(db_path, tok, interval), daemon=True).start()
        def refresh(acct: str) -> bool:
            if scraper.profile_lock(profile_dir(acct)).locked():
                return True                              # the 10-min check is reading this account right now
            out = check_account(DB(db_path), acct, TelegramNotifier(tok, _log_sent(db_path)), _admin_notifier(tok),
                                finished=False)          # the button must be quick; the 10-min check reads 'Completed'
            return "ERROR" not in out
        bot = Bot(Api(tok), DB(db_path), admin_id(), accounts(), solver=make_solver(db_path, tok),
                  refresh=refresh, db_factory=lambda: DB(db_path))
        bot.set_commands()
        print("papaya is running. Ctrl+C to stop.", flush=True)
        offset = None
        while True:
            try:
                offset = bot.poll_once(offset)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("poll error:", type(e).__name__, flush=True)
                time.sleep(5)
    else:
        sys.exit("commands: login <acct> | verify <acct> | check | run")


if __name__ == "__main__":
    main()
