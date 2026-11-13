import pathlib, sys
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
