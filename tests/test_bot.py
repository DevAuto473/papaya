import pathlib, sys
import pytest
from datetime import datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from papaya.db import DB
from papaya.bot import Bot
from papaya.cycle import run_cycle
from papaya.parser import parse_assignments, parse_exams, RIYADH
from tests.test_parser import A, E

ADMIN, KID = 111, 222


class FakeApi:
    def __init__(self): self.calls = []
    def call(self, m, **p): self.calls.append((m, p)); return {}
    def sent(self, chat=None): return [p for m, p in self.calls if m == "sendMessage" and (chat is None or p["chat_id"] == chat)]


def msg(chat, text, name="Sara"): return {"message": {"chat": {"id": chat}, "text": text, "from": {"first_name": name}}}
def cb(chat, data): return {"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": chat}, "message_id": 5}}}


def mk():
    api = FakeApi(); db = DB(":memory:")
    return api, db, Bot(api, db, ADMIN, ["papaya01", "papaya02"])


def test_link_flow_admin_approves():
    import threading
    api, db, bot = mk()                                  # no profile_dir: the form goes to the admin
    api.call = lambda m, **p: api.calls.append((m, p)) or {"message_id": 42}
    timers = []
    real_timer = threading.Timer
    threading.Timer = lambda *a, **k: timers.append(a) or real_timer(9999, lambda: None)
    try:
        for t in ("/start", "Sara", "0501234567", "sara@example.com", "secret123"):
            bot.handle(msg(KID, t))
    finally:
        threading.Timer = real_timer
    req = api.sent(ADMIN)[-1]
    assert "link:papaya02:222" in str(req["reply_markup"])
    assert timers and timers[0][0] == bot.wipe_after     # the admin's copy of the password deletes itself
    assert "وصلت بياناتك" in api.sent(KID)[-1]["text"]  # the student is told it arrived
    bot.handle(cb(KID, "link:papaya01:222"))            # non-admin tapping: ignored
    assert db.chat_id("papaya01") is None
    bot.handle(cb(ADMIN, "link:papaya01:222"))
    assert db.chat_id("papaya01") == KID
    assert "المشرف قبلك" in api.sent(KID)[-1]["text"]         # told the next step (login on the laptop)


def test_signup_auto_login_success(monkeypatch):
    import json
    api, db, bot = mk()
    bot.spawn = lambda fn, *a: fn(*a)                 # run the "background" thread inline for the test
    bot.profile_dir = lambda name: pathlib.Path(f"/tmp/{name}")
    monkeypatch.setattr("papaya.bot.scraper.auto_first_login",
                        lambda profile, email, password: {"assign": "https://x/a", "exams": "https://x/e"})
    bot.handle(msg(KID, "/start"))
    bot.handle(msg(KID, "Sara"))
    bot.handle(msg(KID, "0501234567"))
    bot.handle(msg(KID, "sara@example.com"))
    bot.handle(msg(KID, "secret123"))
    assert db.chat_id("papaya01") == KID               # first free device, picked automatically
    assert json.loads(db.get_meta("urls:papaya01")) == {"assign": "https://x/a", "exams": "https://x/e"}
    assert db.get_meta("welcomed:papaya01") == "1"
    assert any("✅" in (p.get("text") or "") for p in api.sent(ADMIN))  # admin told it worked


def test_signup_creates_a_new_device_when_all_are_full(monkeypatch, tmp_path):
    import json
    api, db, bot = mk()
    bot.spawn = lambda fn, *a: fn(*a)
    bot.profile_dir = lambda name: tmp_path / name
    db.set_chat("papaya01", 901); db.set_chat("papaya02", 902)   # both existing slots already taken
    monkeypatch.setattr("papaya.bot.scraper.auto_first_login",
                        lambda profile, email, password: {"assign": "https://x/a", "exams": "https://x/e"})
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)   # never actually call gh in a test
    bot.handle(msg(KID, "/start"))
    bot.handle(msg(KID, "Noura"))
    bot.handle(msg(KID, "0501111111"))
    bot.handle(msg(KID, "noura@example.com"))
    bot.handle(msg(KID, "secret123"))
    assert "papaya03" in bot.names                              # grew past the original 2
    assert db.chat_id("papaya03") == KID
    assert json.loads(db.get_meta("urls:papaya03")) == {"assign": "https://x/a", "exams": "https://x/e"}


def test_signup_auto_login_failure_frees_the_slot_and_falls_back(monkeypatch):
    api, db, bot = mk()
    bot.spawn = lambda fn, *a: fn(*a)
    bot.profile_dir = lambda name: pathlib.Path(f"/tmp/{name}")

    def boom(profile, email, password):
        raise Exception("boom")
    monkeypatch.setattr("papaya.bot.scraper.auto_first_login", boom)
    bot.handle(msg(KID, "/start"))
    bot.handle(msg(KID, "Sara"))
    bot.handle(msg(KID, "0501234567"))
    bot.handle(msg(KID, "sara@example.com"))
    bot.handle(msg(KID, "secret123"))
    assert db.chat_id("papaya01") is None              # freed back up, never actually logged in
    admin_texts = " ".join(p.get("text") or "" for p in api.sent(ADMIN))
    assert "papaya01" in admin_texts and "login papaya01" in admin_texts


def test_admin_can_change_a_devices_settings():
    api, db, bot = mk(); db.set_chat("papaya01", KID)
    bot.handle(cb(ADMIN, "adm:dev:papaya01"))
    bot.handle(cb(ADMIN, "adm:aset:papaya01:h:1"))
    assert db.get_settings("papaya01").horizon_days == 1
    assert "adm:dev:papaya01" in str(api.calls[-1][1]["reply_markup"])  # back button goes to the device page
    bot.handle(cb(ADMIN, "adm:aset:papaya01:t:notify_new"))
    assert db.get_settings("papaya01").notify_new is False
    bot.handle(cb(KID, "adm:aset:papaya01:h:7"))          # the device's own owner is not the admin: ignored
    assert db.get_settings("papaya01").horizon_days == 1


def test_admin_can_solve_all_for_a_device():
    calls = []

    class FakeSolver:
        def solve_all(self, acct, arg, chat):
            calls.append(("solve_all", acct, chat))
        def submit_all(self, acct, arg, chat):
            calls.append(("submit_all", acct, chat))
    api, db, bot = mk(); db.set_chat("papaya01", KID); db.set_solver("papaya01", True)
    bot.solver = FakeSolver(); bot.spawn = lambda fn, *a: fn(*a)
    bot.handle(cb(ADMIN, "adm:solveall:papaya01"))
    assert calls == [("solve_all", "papaya01", ADMIN)]
    bot.handle(cb(ADMIN, "adm:submitall:papaya01"))
    assert calls[-1] == ("submit_all", "papaya01", ADMIN)
    db.set_solver("papaya01", False)
    bot.handle(cb(ADMIN, "adm:solveall:papaya01"))        # solve privilege off: no-op
    assert len(calls) == 2


def test_settings_roundtrip():
    api, db, bot = mk(); db.set_chat("papaya01", KID)
    bot.handle(cb(KID, "r:6")); bot.handle(cb(KID, "q:0:7")); bot.handle(cb(KID, "t:notify_new"))
    s = db.get_settings("papaya01")
    assert (s.remind_hours, s.quiet_start, s.quiet_end, s.notify_new) == (6, 0, 7, False)
    bot.handle(msg(KID, "/remind 8")); bot.handle(msg(KID, "/quiet off"))
    s = db.get_settings("papaya01"); assert s.remind_hours == 8 and s.quiet_start == s.quiet_end


def test_unlinked_and_non_admin_blocked():
    api, db, bot = mk()
    bot.handle(msg(KID, "/settings")); assert "غير مربوط" in api.sent(KID)[-1]["text"]
    bot.handle(msg(KID, "/status")); assert "غير مربوط" in api.sent(KID)[-1]["text"]   # not admin -> no status
    bot.handle(msg(KID, "/unlink papaya01")); assert db.chat_id("papaya01") is None


def test_bad_args_and_list():
    api, db, bot = mk(); db.set_chat("papaya01", KID)
    bot.handle(msg(KID, "/remind abc")); assert "عدد الساعات" in api.sent(KID)[-1]["text"]
    bot.handle(msg(KID, "/quiet 30 2")); assert "مثال" in api.sent(KID)[-1]["text"]
    now = datetime(2026, 11, 4, 13, 0, tzinfo=RIYADH)
    class R:
        def send(self, c, t, markup=None): return True
    run_cycle(db, "papaya01", parse_assignments(A) + parse_exams(E), R(), now)
    run_cycle(db, "papaya01", parse_assignments(A) + parse_exams(E), R(), now)
    bot.handle(msg(KID, "/list"), now); 
    assert "الواجبات والاختبارات" in api.sent(KID)[-1]["text"]


class FailingApi(FakeApi):
    """Fails chosen methods with a given exception."""
    def __init__(self, fail): super().__init__(); self.fail = fail
    def call(self, m, **p):
        self.calls.append((m, p))
        if m in self.fail:
            raise self.fail[m]
        return {"message_id": 9}


def test_live_list_survives_a_network_blip():
    import urllib.error
    from papaya.bot import ApiError
    api = FailingApi({"editMessageText": urllib.error.URLError("reset")}); db = DB(":memory:")
    bot = Bot(api, db, ADMIN, ["papaya01"]); db.set_chat("papaya01", KID)
    db.set_meta("listmsg:papaya01", f"{KID}:7"); db.set_meta("listsig:papaya01", "old")
    assert bot.update_live_list("papaya01") is False
    assert db.get_meta("listmsg:papaya01") == f"{KID}:7"        # still followed after a blip
    api.fail["editMessageText"] = ApiError("editMessageText", 400, "Bad Request: message to edit not found")
    bot.update_live_list("papaya01")
    assert db.get_meta("listmsg:papaya01") == ""                 # really gone: stop following


def test_settings_tap_on_same_option_is_quiet():
    from papaya.bot import ApiError
    api = FailingApi({"editMessageText": ApiError("editMessageText", 400, "Bad Request: message is not modified")})
    db = DB(":memory:"); bot = Bot(api, db, ADMIN, ["papaya01"]); db.set_chat("papaya01", KID)
    bot.handle(cb(KID, "r:10"))                                  # no exception, no extra message
    assert not api.sent(KID)


def test_list_tap_never_crashes_the_thread():
    import urllib.error
    api = FailingApi({"sendMessage": urllib.error.URLError("reset")}); db = DB(":memory:")
    bot = Bot(api, db, ADMIN, ["papaya01"], refresh=lambda a: False, spawn=lambda fn, *a: fn(*a))
    db.set_chat("papaya01", KID)
    bot.handle(msg(KID, "/list"))                                # Telegram unreachable: must not raise


def test_device_registered_elsewhere_shows_in_admin_panel_without_restart():
    api, db, bot = mk()                                  # bot started knowing only papaya01, papaya02
    db.ensure_account("papaya05")                        # e.g. `login papaya05` ran in the terminal
    assert "papaya05" in bot.names
    bot.handle(cb(ADMIN, "adm:home"))
    assert "adm:dev:papaya05" in str(api.calls[-1][1].get("reply_markup"))


def test_admin_links_a_waiting_person_to_a_free_device():
    import json
    api, db, bot = mk()
    db.ensure_account("papaya05")
    db.set_meta("urls:papaya05", json.dumps({"assign": "a", "exams": "e"}))   # logged in from the terminal
    db.set_meta("pending_name:333", "Khaled")                                   # finished /start earlier
    bot.handle(cb(ADMIN, "adm:dev:papaya05"))
    assert "link:papaya05:333" in str(api.calls[-1][1].get("reply_markup"))
    bot.handle(cb(ADMIN, "link:papaya05:333"))
    assert db.chat_id("papaya05") == 333
    assert db.pending_requests() == []
    assert api.sent(333)                                  # he got the welcome guide


def test_admin_talks_to_a_person_as_the_bot():
    api, db, bot = mk(); db.set_chat("papaya02", KID); db.set_meta("name:papaya02", "Khaled")
    bot.handle(msg(ADMIN, "/talk papaya02"))
    admin_msg = {"message": {"chat": {"id": ADMIN}, "message_id": 77, "text": "هلا خالد"}}
    bot.handle(admin_msg)
    copies = [p for m, p in api.calls if m == "copyMessage"]
    assert copies[-1] == {"chat_id": KID, "from_chat_id": ADMIN, "message_id": 77}   # sent as the bot
    bot.handle({"message": {"chat": {"id": KID}, "message_id": 78, "text": "هلا"}})
    assert "Khaled" in api.sent(ADMIN)[-1]["text"] and "هلا" in api.sent(ADMIN)[-1]["text"]
    assert not any("ما فهمت" in (p.get("text") or "") for p in api.sent(KID))
    bot.handle(msg(KID, "/list"))                         # commands are never relayed
    n = len([1 for m, p in api.calls if m == "copyMessage"])
    bot.handle(msg(ADMIN, "/done"))
    bot.handle({"message": {"chat": {"id": ADMIN}, "message_id": 79, "text": "بعد الإنهاء"}})
    assert len([1 for m, p in api.calls if m == "copyMessage"]) == n             # ended: nothing relayed


def test_admin_acts_as_a_device_owner():
    calls = []

    class FakeSolver:
        prepare = submit = drop = submit_all = lambda self, *a: None
        def solve_all(self, acct, arg, chat): calls.append((acct, chat))
    api, db, bot = mk(); db.set_chat("papaya01", ADMIN); db.set_chat("papaya02", KID)
    db.set_solver("papaya02", True); bot.solver = FakeSolver(); bot.spawn = lambda fn, *a: fn(*a)
    bot.handle(cb(ADMIN, "adm:as:papaya02"))
    bot.handle(msg(ADMIN, "/list"))
    assert "papaya02" in api.sent(ADMIN)[-1]["text"]           # the banner says whose list this is
    assert db.get_meta("listmsg:papaya02") in (None, "")        # the owner's live list is left alone
    bot.handle(cb(ADMIN, "solveall:-"))
    assert calls == [("papaya02", ADMIN)]                        # acts on his device, result comes to me
    bot.handle(cb(ADMIN, "t:notify_new"))
    assert db.get_settings("papaya02").notify_new is False      # settings buttons hit his device too
    assert db.get_settings("papaya01").notify_new is True
    bot.handle(msg(ADMIN, "/me"))
    bot.handle(cb(ADMIN, "solveall:-"))
    assert len(calls) == 1                                       # back on my own (no solver): nothing
    bot.handle(cb(KID, "adm:as:papaya01"))                       # only the admin can do this
    assert bot._account(KID) == "papaya02"


def test_timed_solve_permission_runs_out():
    from datetime import timedelta
    from papaya.bot import parse_duration, duration_ar
    assert parse_duration("3d") == 72 and parse_duration("12h") == 12 and parse_duration("2w") == 336
    assert parse_duration("") == 0 and parse_duration("abc") is None and duration_ar(72) == "3 أيام"
    api, db, bot = mk(); db.set_chat("papaya02", KID)
    bot.handle(cb(ADMIN, "adm:sol:papaya02"))                  # off -> opens the duration picker
    assert "adm:solt:papaya02:72" in str(api.calls[-1][1].get("reply_markup"))
    bot.handle(cb(ADMIN, "adm:solt:papaya02:72"))
    assert db.solver_enabled("papaya02")
    assert "صار عندك ميزة الحل" in api.sent(KID)[-1]["text"]
    later = datetime.now(RIYADH) + timedelta(hours=73)
    assert not db.solver_enabled("papaya02", later)           # 3 days later: gone by itself
    bot.expire_check(later)
    assert "انتهت" in api.sent(KID)[-1]["text"] and "انتهت" in api.sent(ADMIN)[-1]["text"]
    n = len(api.sent(KID)); bot.expire_check(later)
    assert len(api.sent(KID)) == n                              # announced only once
    bot.handle(msg(ADMIN, "/grant papaya02"))                   # no duration: permanent
    assert db.solver_enabled("papaya02", later + timedelta(days=400))


def test_free_text_reaches_admin_and_admin_answers_with_reply():
    api, db, bot = mk(); db.set_chat("papaya02", KID); db.set_meta("name:papaya02", "Khaled")
    api.call = lambda m, **p: api.calls.append((m, p)) or {"message_id": 500 + len(api.calls)}
    bot.handle({"message": {"chat": {"id": KID}, "message_id": 10, "text": "البوت ما نبهني اليوم"}})
    fwd = api.sent(ADMIN)[-1]
    assert "Khaled" in fwd["text"] and "ما نبهني" in fwd["text"]
    assert not any("ما فهمت" in (p.get("text") or "") for p in api.sent(KID))
    fwd_id = int(db.con.execute("SELECT k FROM meta WHERE k LIKE 'fwd:%'").fetchone()["k"].split(":")[1])
    bot.handle({"message": {"chat": {"id": ADMIN}, "message_id": 11, "text": "تمام بشيك عليه",
                            "reply_to_message": {"message_id": fwd_id}}})
    assert [p for m, p in api.calls if m == "copyMessage"][-1] == {"chat_id": KID, "from_chat_id": ADMIN, "message_id": 11}
    bot.handle({"message": {"chat": {"id": KID}, "message_id": 12, "photo": [{"file_id": "x"}]}})   # photos too
    assert [p for m, p in api.calls if m == "copyMessage"][-1]["chat_id"] == ADMIN
    bot.handle(msg(KID, "/xyz"))                                  # unknown command: still 'ما فهمت'
    assert "ما فهمت" in api.sent(KID)[-1]["text"]


def test_admin_message_gets_a_thumbs_up_when_delivered():
    api, db, bot = mk(); db.set_chat("papaya02", KID)
    bot.handle(msg(ADMIN, "/talk papaya02"))
    bot.handle({"message": {"chat": {"id": ADMIN}, "message_id": 90, "text": "هلا"}})
    assert ("setMessageReaction", {"chat_id": ADMIN, "message_id": 90,
            "reaction": [{"type": "emoji", "emoji": "👍"}]}) in api.calls
    copy = [p for m, p in api.calls if m == "copyMessage"][-1]
    assert "disable_notification" not in copy                   # a normal, notifying message


def test_clear_is_not_blocked_by_one_undeletable_message():
    from datetime import timedelta
    api, db, bot = mk(); db.set_chat("papaya02", KID); bot.spawn = lambda fn, *a: fn(*a)
    now = datetime.now(RIYADH)
    db.log_msg(KID, 3, now - timedelta(hours=60))               # too old for Telegram: not even tried
    for i in range(10, 20):
        db.log_msg(KID, i, now)
    deleted = set()

    def call(m, **p):
        api.calls.append((m, p))
        if m == "deleteMessages":
            if 15 in p["message_ids"]:                           # Telegram refuses any batch holding #15
                raise Exception("Bad Request: message can't be deleted")
            deleted.update(p["message_ids"])
        return {"message_id": 99}
    api.call = call
    bot.handle({"message": {"chat": {"id": KID}, "message_id": 20, "text": "/clear"}})
    assert set(range(10, 21)) - {15} <= deleted                  # everything else still went
    assert 3 not in deleted
    assert "Clear history" in api.sent(KID)[-1]["text"]          # tells them about the 48h limit


def test_a_timeout_is_never_sent_twice(monkeypatch):
    """Waiting for Telegram's answer timed out: the message may already be in the chat."""
    import urllib.request, urllib.error
    from papaya.bot import Api
    calls = []
    def boom(req, timeout=0):
        calls.append(req.full_url)
        raise urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.URLError):
        Api("T").call("sendMessage", chat_id=1, text="x")
    assert len(calls) == 1


def test_a_connection_failure_is_still_retried(monkeypatch):
    import urllib.request, urllib.error, papaya.bot as b
    calls = []
    def boom(req, timeout=0):
        calls.append(1)
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    with pytest.raises(urllib.error.URLError):
        b.Api("T").call("sendMessage", chat_id=1, text="x")
    assert len(calls) == 3


# ---- one person must never be able to freeze the bot ----------------------------------
def _sync(bot, t0=1000.0):
    bot.spawn = lambda fn, *a: fn(*a)
    clock = [t0]; bot.clock = lambda: clock[0]
    return clock


def test_clear_spam_runs_once_then_waits_for_the_cooldown():
    api, db, bot = mk(); db.set_chat("papaya02", KID); clock = _sync(bot)
    for i in range(10):
        bot.handle({"message": {"chat": {"id": KID}, "message_id": 50 + i, "text": "/clear"}})
    assert sum(1 for t in api.sent(KID) if "نظّفت" in t["text"]) == 1
    clock[0] += 31                                          # cooldown over: works again
    bot.handle({"message": {"chat": {"id": KID}, "message_id": 70, "text": "/clear"}})
    assert sum(1 for t in api.sent(KID) if "نظّفت" in t["text"]) == 2


def test_clear_left_over_from_before_a_restart_is_skipped():
    api, db, bot = mk(); db.set_chat("papaya02", KID); clock = _sync(bot)
    bot.handle({"message": {"chat": {"id": KID}, "message_id": 5, "text": "/clear", "date": int(clock[0]) - 600}})
    assert not api.sent(KID)


def test_flooding_chat_is_muted_but_the_admin_never_is():
    api, db, bot = mk(); db.set_chat("papaya02", KID); clock = _sync(bot)
    for i in range(25):
        bot.handle({"message": {"chat": {"id": KID}, "message_id": i + 1, "text": "/help"}})
    helps = sum(1 for t in api.sent(KID) if "الأزرار" in t["text"])
    assert helps == 20 and any("رسائل كثيرة" in t["text"] for t in api.sent(KID))
    clock[0] += 121                                         # mute over
    bot.handle({"message": {"chat": {"id": KID}, "message_id": 99, "text": "/help"}})
    assert sum(1 for t in api.sent(KID) if "الأزرار" in t["text"]) == 21
    for i in range(30):
        bot.handle({"message": {"chat": {"id": ADMIN}, "message_id": 200 + i, "text": "/help"}})
    assert not any("رسائل كثيرة" in t["text"] for t in api.sent(ADMIN))


def test_a_burst_of_the_same_command_in_one_batch_runs_once():
    api, db, bot = mk(); db.set_chat("papaya02", KID); _sync(bot)
    burst = [{"update_id": i, "message": {"chat": {"id": KID}, "message_id": i, "text": "/help"}} for i in range(1, 8)]
    burst.append({"update_id": 9, "message": {"chat": {"id": KID}, "message_id": 9, "text": "شكراً"}})
    real = api.call
    api.call = lambda m, **p: burst if m == "getUpdates" else real(m, **p)
    assert bot.poll_once(None) == 10
    assert sum(1 for t in api.sent(KID) if "الأزرار" in t["text"]) == 1
