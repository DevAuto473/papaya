import copy, pathlib, sys
from datetime import datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from papaya.db import DB
from papaya.cycle import run_cycle
from papaya.parser import parse_assignments, parse_exams, RIYADH
from tests.test_parser import A, E


class Rec:
    def __init__(self, ok=True): self.msgs, self.kbs, self.ok = [], [], ok
    def send(self, chat, text, markup=None): self.msgs.append(text); self.kbs.append(markup); return self.ok


def mk():
    db = DB(":memory:"); db.set_chat("papaya01", 1); return db


def T(d, h, m=0): return datetime(2026, 11, d, h, m, tzinfo=RIYADH)
def items(): return parse_assignments(A) + parse_exams(E)
def fresh(base, **kw):
    it = copy.copy(base); it.__dict__.update(kw); return it


def test_baseline_only_open_items_due_today_or_tomorrow():
    db, r = mk(), Rec()
    run_cycle(db, "papaya01", items(), r, T(4, 13, 50))
    assert len(r.msgs) == 1 and "تم ربط الحساب" in r.msgs[0]
    assert "🔒" not in r.msgs[0].split("<i>")[0]          # no locked items (legend aside)
    assert "الجمعة" not in r.msgs[0]                        # nothing beyond tomorrow
    assert r.kbs[0] and all("url" in row[0] or row[0]["callback_data"].startswith("done:")
                            for row in r.kbs[0]["inline_keyboard"])
    run_cycle(db, "papaya01", items(), r, T(4, 14, 20))     # baseline covered them: no heads-up repeat
    assert not any(m.startswith(("🆕", "📅")) for m in r.msgs[1:])


def test_remind_once_within_10h_with_button():
    db, r = mk(), Rec()
    run_cycle(db, "papaya01", items(), r, T(4, 13, 0)); r.msgs.clear(); r.kbs.clear()
    run_cycle(db, "papaya01", items(), r, T(4, 23, 0))      # quiet hours -> nothing
    assert not r.msgs
    run_cycle(db, "papaya01", items(), r, T(5, 6, 0))
    assert len(r.msgs) == 1 and "مواعيد قريبة" in r.msgs[0]      # morning: one grouped message
    assert "حل الواجب" in str(r.kbs[0]) or "اختبار" in str(r.kbs[0])
    n = len(r.msgs); run_cycle(db, "papaya01", items(), r, T(5, 6, 30))
    assert len(r.msgs) == n, "no repeats"


def test_far_item_is_silent_until_it_enters_the_window():
    db, r = mk(), Rec(); its = items()
    run_cycle(db, "papaya01", its, r, T(4, 13, 0)); r.msgs.clear()
    far = fresh(its[2], id="FAR", title="واجب بعيد", deadline=T(9, 12))
    run_cycle(db, "papaya01", its + [far], r, T(4, 15, 0))
    assert not any("واجب بعيد" in m for m in r.msgs)
    run_cycle(db, "papaya01", its + [far], r, T(8, 9, 0))   # now 'tomorrow'
    assert ("assignment", "FAR", "new") in db.sent_events("papaya01") and r.msgs


def test_new_item_soon_alerts_and_quiet_hours_defer():
    db, r = mk(), Rec(); its = items()
    run_cycle(db, "papaya01", its, r, T(4, 13, 0)); r.msgs.clear()
    new = fresh(its[2], id="NEWID", title="واجب جديد", deadline=T(5, 20))
    run_cycle(db, "papaya01", its + [new], r, T(4, 23, 30))
    assert not r.msgs
    run_cycle(db, "papaya01", its + [new], r, T(5, 6, 5))   # morning: may arrive inside the grouped message
    assert r.msgs and ("assignment", "NEWID", "new") in db.sent_events("papaya01")


def test_locked_item_never_alerts_then_opened_alerts():
    db, r = mk(), Rec(); its = items()
    run_cycle(db, "papaya01", its, r, T(4, 13, 0)); r.msgs.clear()
    waiting = next(i for i in its if i.kind == "exam" and not i.is_open)
    locked = fresh(waiting, deadline=T(6, 9))
    run_cycle(db, "papaya01", [i if i is not waiting else locked for i in its], r, T(5, 8, 0))
    assert not any(locked.title in m and locked.subject in m and "🔓" in m for m in r.msgs)
    opened = fresh(locked, status="بانتظار حل الاختبار")
    run_cycle(db, "papaya01", [i if i is not waiting else opened for i in its], r, T(5, 10, 0))
    assert any("انفتح الآن 🔓" in m for m in r.msgs)


def test_failed_send_retries():
    db = mk(); its = items()
    run_cycle(db, "papaya01", its, Rec(), T(4, 13, 0))
    new = fresh(its[2], id="NEWID", deadline=T(5, 12))
    run_cycle(db, "papaya01", its + [new], Rec(ok=False), T(4, 15, 0))
    good = Rec(); run_cycle(db, "papaya01", its + [new], good, T(4, 15, 30))
    assert any("جديد 🆕" in m.split(chr(10))[0] for m in good.msgs)


def test_many_at_once_is_one_grouped_message():
    db, r = mk(), Rec(); its = items()
    run_cycle(db, "papaya01", its, r, T(4, 13, 0)); r.msgs.clear()
    burst = [fresh(its[2], id=f"B{n}", deadline=T(5, 20)) for n in range(5)]
    run_cycle(db, "papaya01", its + burst, r, T(4, 15, 0))
    assert sum("مواعيد قريبة" in m for m in r.msgs) == 1 and not any("جديد 🆕" in m for m in r.msgs)
    r.msgs.clear(); run_cycle(db, "papaya01", its + burst, r, T(4, 15, 30))
    assert not any("مواعيد قريبة" in m for m in r.msgs)


def test_settings_horizon_all_and_hide_external():
    db, r = mk(), Rec(); its = items()
    db.set_setting("papaya01", "horizon_days", 0)
    db.set_setting("papaya01", "remind_external", 0)
    run_cycle(db, "papaya01", its, r, T(4, 13, 0))
    assert "الجمعة" in r.msgs[0] and "📨 واجب" not in r.msgs[0]


def test_unlinked_account_sends_nothing():
    db = DB(":memory:"); r = Rec()
    assert run_cycle(db, "papaya02", items(), r, T(4, 13, 0)) == 0 and not r.msgs


def test_old_database_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE settings(account TEXT PRIMARY KEY, remind_hours REAL NOT NULL DEFAULT 10,
      quiet_start INTEGER NOT NULL DEFAULT 23, quiet_end INTEGER NOT NULL DEFAULT 6, notify_new INTEGER NOT NULL DEFAULT 1,
      notify_remind INTEGER NOT NULL DEFAULT 1, notify_opened INTEGER NOT NULL DEFAULT 1, remind_external INTEGER NOT NULL DEFAULT 1);
      INSERT INTO settings(account) VALUES('papaya01');""")
    con.commit(); con.close()
    assert DB(path).get_settings("papaya01").horizon_days == 2


def test_single_reminder_keeps_its_own_button():
    db, r = mk(), Rec(); its = items()
    run_cycle(db, "papaya01", its, r, T(4, 12, 0)); r.msgs.clear(); r.kbs.clear()
    one = fresh(its[2], id="ONE", title="واجب وحيد", deadline=T(4, 20))
    run_cycle(db, "papaya01", its + [one], r, T(4, 13, 0))
    assert len(r.msgs) == 1 and r.msgs[0].split(chr(10))[0].endswith("⏳") and "حل الواجب" in str(r.kbs[0])