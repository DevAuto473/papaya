"""The emergency auto-solve (last 5 hours) for several devices at once, with the admin's
'solve for everyone' switch. Homework follows the switch; exams follow each device's own grant."""
import json
import pathlib
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from papaya import db as dbmod
from papaya.cycle import run_cycle
from papaya.db import DB
from papaya.parser import parse_assignments, parse_exams
from papaya.reminders import list_buttons
from papaya.service import AUTO_SOLVE_BEFORE, SolveService
from tests.test_parser import A, E
from tests.test_solver import ADMIN, Adapter, Notif, T, reply

CHATS = {"papaya01": 222, "papaya02": 333, "papaya03": 444}
NOW = T(5, 4, 0)


def world(monkeypatch, everyone=True, grants=()):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    db = DB(":memory:")
    db.set_everyone(everyone)
    for acct, chat in CHATS.items():
        db.set_chat(acct, chat)
        run_cycle(db, acct, parse_assignments(A) + parse_exams(E), Notif(), T(4, 13))
    for g in grants:
        db.set_solver(g, True)
    n, ad = Notif(), Adapter()
    svc = SolveService(lambda: db, n, lambda: ADMIN, lambda a: pathlib.Path("/tmp/x"),
                       ask=lambda m, msgs: (reply([2, 2]), 0.01),
                       page_runner=lambda acct, url, fn: fn(object()), adapter=ad,
                       models=("google/gemini-x:online", "anthropic/haiku-x"), now_fn=lambda: NOW)
    return db, svc, n


def due_soon(kind):
    """Independent of the service: ids of open, on-platform items of this kind due within 5 hours."""
    items = parse_assignments(A) if kind == "assignment" else parse_exams(E)
    return {i.id for i in items
            if i.is_open and i.solve_url and not getattr(i, "external_submission", False)
            and timedelta(0) < i.deadline - NOW <= AUTO_SOLVE_BEFORE}


def cycles(svc, accounts, n=6):
    for _ in range(n):
        for a in accounts:
            svc.auto(a)


def solved(db, acct):
    return {r["item_id"] for r in db.con.execute("select item_id from solutions") if db.solution(acct, r["item_id"])}


def test_the_scenario_really_has_homework_and_exams_in_the_window():
    assert due_soon("assignment") and due_soon("exam")


def test_everyone_gets_the_emergency_solve_for_homework_and_never_for_exams(monkeypatch):
    db, svc, n = world(monkeypatch, everyone=True)                  # nobody was granted anything
    cycles(svc, CHATS)
    hw, ex = due_soon("assignment"), due_soon("exam")
    for acct, chat in CHATS.items():
        got = solved(db, acct)
        assert got == hw, f"{acct}: solved {got}, expected exactly the homework {hw}"
        assert not got & ex
        assert any("كل الإجابات" in t or "حل" in t for t in n.to(chat)), f"{acct} was not told"


def test_a_granted_device_keeps_its_exam_solving(monkeypatch):
    db, svc, n = world(monkeypatch, everyone=True, grants=["papaya02"])
    cycles(svc, CHATS)
    hw, ex = due_soon("assignment"), due_soon("exam")
    assert solved(db, "papaya02") == hw | ex                        # unchanged: its own grant covers exams
    assert solved(db, "papaya01") == hw and solved(db, "papaya03") == hw


def test_exam_by_hand_is_refused_for_ungranted_devices(monkeypatch):
    db, svc, n = world(monkeypatch, everyone=True)
    exam_id = next(iter(due_soon("exam")))
    assert svc.prepare("papaya01", exam_id, 222) == "invalid"
    assert any("اختبار" in t and "بنفسك" in t for t in n.to(222))
    assert db.solution("papaya01", exam_id) is None


def test_switch_off_means_only_granted_devices(monkeypatch):
    db, svc, n = world(monkeypatch, everyone=False, grants=["papaya03"])
    cycles(svc, CHATS)
    assert solved(db, "papaya01") == set() and solved(db, "papaya02") == set()
    assert solved(db, "papaya03") == due_soon("assignment") | due_soon("exam")


def test_buttons_follow_the_same_rule(monkeypatch):
    db, svc, n = world(monkeypatch, everyone=True, grants=["papaya02"])
    items = parse_assignments(A) + parse_exams(E)
    plain = json.dumps(list_buttons(items, T(4, 14), solve_ok=db.solver_enabled("papaya01"),
                                    exam_ok=db.solver_granted("papaya01")))
    full = json.dumps(list_buttons(items, T(4, 14), solve_ok=db.solver_enabled("papaya02"),
                                   exam_ok=db.solver_granted("papaya02")))
    hw_ids = {i.id for i in parse_assignments(A)}
    ex_ids = {i.id for i in parse_exams(E)}
    assert any(f"solve:{i}" in plain for i in hw_ids) and not any(f"solve:{i}" in plain for i in ex_ids)
    assert any(f"solve:{i}" in full for i in ex_ids)                 # the granted device still gets exam buttons


def test_a_new_device_has_the_solver_from_the_start_with_the_real_default(monkeypatch):
    monkeypatch.setattr(dbmod, "SOLVE_EVERYONE_DEFAULT", "1")        # the shipped default (the suite overrides it)
    db = DB(":memory:")
    db.ensure_account("papaya09")
    assert db.solver_enabled("papaya09") and not db.solver_granted("papaya09")
    assert not db.solver_enabled("ghost")                            # a name that is no device gets nothing
    assert 'SOLVE_EVERYONE_DEFAULT = "1"' in pathlib.Path(dbmod.__file__).read_text(encoding="utf-8")


def mkbot():
    from tests.test_bot import mk
    return mk()


def test_admin_switch_by_command_and_button_and_nobody_else(monkeypatch):
    api, db, bot = mkbot()
    db.set_everyone(True)
    bot.handle({"message": {"chat": {"id": 222}, "message_id": 1, "text": "/everyone off"}})    # not the admin
    assert db.everyone_solves()
    bot.handle({"message": {"chat": {"id": ADMIN}, "message_id": 2, "text": "/everyone off"}})
    assert not db.everyone_solves() and not db.solver_enabled("papaya01")
    bot.handle({"callback_query": {"id": "1", "data": "adm:everyone",
                                   "message": {"chat": {"id": ADMIN}, "message_id": 3}}})
    assert db.everyone_solves() and db.solver_enabled("papaya01")
    bot.handle({"callback_query": {"id": "2", "data": "adm:everyone",           # not the admin: ignored
                                   "message": {"chat": {"id": 222}, "message_id": 4}}})
    assert db.everyone_solves()


def test_a_timed_grant_ending_is_not_announced_while_everyone_has_it():
    api, db, bot = mkbot()
    db.set_chat("papaya01", 222)
    from datetime import datetime
    from papaya.parser import RIYADH
    t0 = datetime.now(RIYADH)
    db.set_everyone(True); db.set_solver("papaya01", True, until=t0 + timedelta(hours=1))
    bot.expire_check(t0 + timedelta(hours=2))
    assert not [m for m in api.sent(222) if "انتهت" in m["text"]]
    db.set_everyone(False); db.set_solver("papaya01", True, until=t0 + timedelta(hours=1))
    bot.expire_check(t0 + timedelta(hours=2))
    assert [m for m in api.sent(222) if "انتهت" in m["text"]]
