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
    api, db, bot = mk()
    bot.handle(msg(KID, "/start"))
    req = api.sent(ADMIN)[-1]
    assert "link:papaya02:222" in str(req["reply_markup"])
    bot.handle(cb(KID, "link:papaya01:222"))            # non-admin tapping: ignored
    assert db.chat_id("papaya01") is None
    bot.handle(cb(ADMIN, "link:papaya01:222"))
    assert db.chat_id("papaya01") == KID
    assert "papaya01" in api.sent(KID)[-1]["text"]


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
