import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from papaya.db import DB
from papaya.main import check_account
from papaya import scraper
from tests.test_parser import A, E


class N:
    def __init__(s): s.m = []
    def send(s, c, t, markup=None): s.m.append(t); return True


def test_ok_then_session_expired_alerts_once_then_recovers():
    db = DB(":memory:"); db.set_chat("papaya01", 1); n = N(); admin = []
    assert "26" in check_account(db, "papaya01", n, admin.append, fetch=lambda: (A, E))
    def expired(): raise scraper.SessionExpired("انتهت الجلسة")
    check_account(db, "papaya01", n, admin.append, fetch=expired)
    check_account(db, "papaya01", n, admin.append, fetch=expired)
    assert len(admin) == 1 and "login papaya01" in admin[0]
    check_account(db, "papaya01", n, admin.append, fetch=lambda: (A, E))
    assert len(admin) == 2 and "رجع يشتغل" in admin[1]


def test_unexpected_page_never_looks_like_empty():
    from papaya.parser import page_title
    assert page_title(A) == "واجباتي" and page_title(E) == "اختباراتي" and page_title("<html></html>") == ""
