import json, pathlib, sys, types
from datetime import datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from papaya.db import DB
from papaya.bot import Bot, settings_view
from papaya.cycle import run_cycle
from papaya.parser import parse_assignments, parse_exams, RIYADH
from papaya.service import SolveService
from papaya.solver import Question, solve
from papaya.scraper import ScrapeError
from tests.test_parser import A, E

ADMIN, KID = 111, 222
T = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=RIYADH)
QS = [Question("q1", "ناتج 2+2؟", "choice", ["3", "4", "5"]),
      Question("q2", "عاصمة السعودية؟", "choice", ["جدة", "الرياض"])]


def reply(choices):
    return json.dumps({"answers": [{"id": f"q{i+1}", "choice": c, "text": None, "why": "سبب"}
                                   for i, c in enumerate(choices)]})


class Notif:
    def __init__(self): self.sent = []
    def send(self, chat, text, markup=None, silent=False): self.sent.append((chat, text, markup)); return True
    def to(self, chat): return [t for c, t, _ in self.sent if c == chat]
    def kb(self, chat): return [k for c, _, k in self.sent if c == chat][-1]


class Adapter:
    def __init__(self): self.submitted = None; self.fail = False
    def read_questions(self, page, allow_exam=False): self.allow_exam = allow_exam; return QS
    def fill_and_submit(self, page, qs, ans, allow_exam=False):
        self.allow_exam = allow_exam
        if self.fail: raise ScrapeError("زر التسليم ما ظهر", "submit button missing")
        self.submitted = [a.choice for a in ans]


def setup(answers_a=(2, 2), answers_b=(2, 2), grant=True, now=T(4, 15), db=None):
    db = db or DB(":memory:"); db.set_chat("papaya01", KID)
    run_cycle(db, "papaya01", parse_assignments(A) + parse_exams(E), Notif(), T(4, 13))
    if grant: db.set_solver("papaya01", True)
    calls = []
    def ask(model, msgs):
        calls.append(model)
        return reply(answers_a if "gemini" in model else answers_b), 0.01
    n, ad = Notif(), Adapter()
    svc = SolveService(lambda: db, n, lambda: ADMIN, lambda a: pathlib.Path("/tmp/x"), ask=ask,
                       page_runner=lambda acct, url, fn: fn(object()), adapter=ad,
                       models=("google/gemini-x:online", "anthropic/haiku-x"), now_fn=lambda: now)
    open_id = next(i.id for i in parse_assignments(A) if i.is_open and i.solve_url and not i.external_submission)
    return db, svc, n, ad, calls, open_id


def test_compare_agree_and_disagree():
    ask = lambda m, msgs: (reply([2, 2] if "a" == m else [2, 1]), 0.0)
    r = solve(QS, "رياضيات", "واجب", ask, "a", "b")
    assert [a.agree for a in r.answers] == [True, False] and r.answers[1].alt_choice == 1 and not r.all_agree


def test_one_model_down_means_no_green_ticks():
    def ask(m, msgs):
        if m == "b": raise RuntimeError("down")
        return reply([2, 2]), 0.0
    r = solve(QS, "", "", ask, "a", "b")
    assert all(a.choice == 2 for a in r.answers) and not any(a.agree for a in r.answers) and r.errors


def test_prepare_then_submit_flow_and_admin_is_told():
    db, svc, n, ad, calls, iid = setup()
    assert svc.prepare("papaya01", iid, KID) == "ready"
    last_kb = n.kb(KID)
    assert "submit:" in json.dumps(last_kb) and "كل الإجابات" in n.to(KID)[-1]
    assert any("حل" in t for t in n.to(ADMIN)) and len(calls) == 2
    assert svc.prepare("papaya01", iid, KID) == "resent" and len(calls) == 2      # no second AI cost
    assert svc.submit("papaya01", iid, KID) == "submitted" and ad.submitted == [2, 2]
    assert any("سلّم" in t for t in n.to(ADMIN))
    assert svc.submit("papaya01", iid, KID) == "nosolution"


def test_without_permission_nothing_happens_at_all():
    db, svc, n, ad, calls, iid = setup(grant=False)
    assert svc.prepare("papaya01", iid, KID) == "denied" and not n.sent and not calls
    assert svc.submit("papaya01", iid, KID) == "denied"


def test_homework_style_exams_are_solved_like_assignments():
    db, svc, n, ad, calls, _ = setup()
    exam_id = next(i.id for i in parse_exams(E) if i.is_open and i.solve_url)
    assert svc.prepare("papaya01", exam_id, KID) == "ready" and calls and ad.allow_exam is True
    assert svc.submit("papaya01", exam_id, KID) == "submitted" and ad.submitted == [2, 2]
    assert ("exam", exam_id) in db.done_keys("papaya01")


def test_budget_cap_blocks_and_tells_admin():
    db, svc, n, ad, calls, iid = setup()
    db.add_spend("2026-11", 10.0)
    assert svc.prepare("papaya01", iid, KID) == "budget" and not calls
    assert any("سقف" in t for t in n.to(ADMIN))


def test_failed_submit_keeps_solution_and_gives_link():
    db, svc, n, ad, calls, iid = setup()
    svc.prepare("papaya01", iid, KID); ad.fail = True
    assert svc.submit("papaya01", iid, KID) == "failed"
    assert "افتح الواجب" in json.dumps(n.kb(KID), ensure_ascii=False)
    ad.fail = False
    assert svc.submit("papaya01", iid, KID) == "submitted"


def test_auto_solve_5h_before_and_auto_submit_only_if_all_agree(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    # assignment due 11-05 08:21 -> 5h window opens 03:21
    db, svc, n, ad, calls, _ = setup(answers_b=(2, 1), now=T(5, 2, 0))
    assert svc.auto("papaya01") == []                        # 6h21 left: too early
    svc.now = lambda: T(5, 4, 0)
    r = svc.auto("papaya01"); assert r and set(r) == {"ready"}
    db.set_setting("papaya01", "auto_submit", 1)
    svc.now = lambda: T(5, 7, 40)
    assert "submitted" not in svc.auto("papaya01")           # disagreement -> never auto-submits


def test_auto_submit_when_everything_agrees(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    db, svc, n, ad, calls, _ = setup(now=T(5, 4, 0))
    db.set_setting("papaya01", "auto_submit", 1)
    r = svc.auto("papaya01"); assert r and set(r) == {"ready"}
    svc.now = lambda: T(5, 7, 40)
    r = svc.auto("papaya01"); assert "submitted" in r and ad.submitted


class FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None): return T(4, 15)


def freeze_bot_clock(monkeypatch):
    monkeypatch.setattr("papaya.bot.datetime", FrozenDT)


def listed_exam(bot):
    """An exam that is really shown (with its 🆘) in the list at the frozen time."""
    exams = {i.id for i in parse_exams(E)}
    kb = bot.list_view("papaya01", T(4, 15))[1]
    ids = [b["callback_data"][6:] for r in kb["inline_keyboard"] for b in r
           if b.get("callback_data", "").startswith("solve:")]
    return next(i for i in ids if i in exams)


class FakeApi:
    def __init__(self): self.calls = []
    def call(self, m, **p): self.calls.append((m, p)); return {}


def test_button_and_settings_invisible_without_grant_and_admin_controls():
    db = DB(":memory:"); db.set_chat("papaya01", KID)
    api = FakeApi(); ran = []
    bot = Bot(api, db, ADMIN, ["papaya01"], solver=types.SimpleNamespace(
        prepare=lambda *a: ran.append(a), submit=lambda *a: None, drop=lambda *a: None), spawn=lambda fn, *a: fn(*a))
    n = Notif(); run_cycle(db, "papaya01", parse_assignments(A) + parse_exams(E), n, T(4, 13))
    assert "solve:" not in json.dumps(n.sent)                               # no AI button
    assert "auto_solve" not in json.dumps(settings_view(db, "papaya01")[1])
    cb = lambda chat, data: {"callback_query": {"id": "1", "data": data, "message": {"chat": {"id": chat}, "message_id": 1}}}
    bot.handle(cb(KID, "solve:ABC")); assert ran == []                     # forged tap ignored
    msg = lambda chat, t: {"message": {"chat": {"id": chat}, "text": t, "from": {}}}
    bot.handle(msg(KID, "/grant papaya01")); assert not db.solver_enabled("papaya01")   # not admin
    bot.handle(msg(ADMIN, "/grant papaya01")); assert db.solver_enabled("papaya01")
    bot.handle(cb(KID, "solve:ABC")); assert ran == [("papaya01", "ABC", KID)]
    assert "auto_solve" in json.dumps(settings_view(db, "papaya01")[1])
    bot.handle(msg(ADMIN, "/revoke papaya01")); ran.clear()
    bot.handle(cb(KID, "solve:ABC")); assert ran == [] and not db.solver_enabled("papaya01")


def test_ai_button_appears_after_grant():
    db = DB(":memory:"); db.set_chat("papaya01", KID); db.set_solver("papaya01", True)
    n = Notif(); run_cycle(db, "papaya01", parse_assignments(A) + parse_exams(E), n, T(4, 13))
    s = json.dumps(n.sent, ensure_ascii=False)
    assert "solve:" in s
    exam_ids = [i.id for i in parse_exams(E)]
    assert any(f"solve:{x}" in s for x in exam_ids)                        # exams (homework-style) get it too


def test_match_and_order_only_assignment_is_not_treated_as_failure():
    from papaya.solver import Question as Q
    qs = [Q("q1", "زاوج", "match", ["x", "y"], prompts=["a", "b"]), Q("q2", "رتب", "order", ["1", "2", "3"])]
    db, svc, n, ad, calls, iid = setup()
    ad.read_questions = lambda page: qs
    svc.ask = lambda m, msgs: (json.dumps({"answers": [{"id": "q1", "seq": [2, 1], "why": ""},
                                                       {"id": "q2", "seq": [3, 1, 2], "why": ""}]}), 0.0)
    assert svc.prepare("papaya01", iid, KID) == "ready"
    assert "مزاوجة" in n.to(KID)[-1] and "الترتيب" in n.to(KID)[-1]


# ---------------- v5: unconfirmed submit, double taps, direct submit, solve-all, /clear --------------
def test_unconfirmed_submit_is_success_when_assignment_left_the_list():
    from papaya.assignment_page import Unconfirmed
    db, svc, n, ad, calls, iid = setup()
    svc.prepare("papaya01", iid, KID)
    def unconfirmed(page, qs, ans): raise Unconfirmed()
    ad.fill_and_submit = unconfirmed
    svc.verify_fn = lambda acct, item: True                     # gone from 'Current'
    assert svc.submit("papaya01", iid, KID) == "submitted"
    assert "تم التسليم" in n.to(KID)[-1]


def test_unconfirmed_and_still_listed_is_reported_as_failure():
    from papaya.assignment_page import Unconfirmed
    db, svc, n, ad, calls, iid = setup()
    svc.prepare("papaya01", iid, KID)
    def unconfirmed(page, qs, ans): raise Unconfirmed()
    ad.fill_and_submit = unconfirmed
    svc.verify_fn = lambda acct, item: False
    assert svc.submit("papaya01", iid, KID) == "failed"


def test_double_tap_is_ignored_while_busy():
    db, svc, n, ad, calls, iid = setup()
    svc.prepare("papaya01", iid, KID)
    assert svc._claim(("papaya01", iid))                       # a submit is "in progress"
    assert svc.submit("papaya01", iid, KID) == "busy"
    svc._release(("papaya01", iid))
    assert svc.submit("papaya01", iid, KID) == "submitted"


def test_direct_submit_skips_the_preview():
    db, svc, n, ad, calls, iid = setup()
    db.set_setting("papaya01", "direct_submit", 1)
    assert svc.prepare("papaya01", iid, KID) == "submitted" and ad.submitted == [2, 2]
    assert not any("الحل جاهز" in t for t in n.to(KID))


def test_solve_all_then_submit_all_one_summary_each():
    db, svc, n, ad, calls, iid = setup()
    n.sent.clear()
    assert svc.solve_all("papaya01", "-", KID) == "done"
    opened = [r for r in svc._listed_items(db, "papaya01")]
    assert len(calls) == 2 * len(opened) and len(opened) >= 3
    summary = n.to(KID)[-1]
    assert "الحلول جاهزة" in summary and "submitall" in json.dumps(n.kb(KID))
    assert svc.submit_all("papaya01", "-", KID) == "done"
    assert "التسليم" in n.to(KID)[-1] and n.to(KID)[-1].count("📤") >= len(opened)
    assert all(db.solution("papaya01", r["id"])["status"] == "submitted" for r in opened)


def test_solve_all_with_direct_submit_does_everything_in_one_go():
    db, svc, n, ad, calls, iid = setup()
    db.set_setting("papaya01", "direct_submit", 1)
    assert svc.solve_all("papaya01", "-", KID) == "done"
    assert "النتيجة" in n.to(KID)[-1] and "submitall" not in json.dumps(n.kb(KID) or {})


def test_clear_deletes_logged_messages_and_settings_toggle_needs_grant():
    db = DB(":memory:"); db.set_chat("papaya01", KID)
    api = FakeApi()
    class A2(FakeApi):
        def call(self, m, **p):
            super().call(m, **p); return {"message_id": len(self.calls) + 100}
    api = A2()
    bot = Bot(api, db, ADMIN, ["papaya01"])
    msg = lambda t, mid: {"message": {"chat": {"id": KID}, "text": t, "from": {}, "message_id": mid}}
    bot.handle(msg("/help", 5)); bot.handle(msg("/list", 6))
    assert len(db.chat_msgs(KID)) == 4                           # 2 incoming + 2 replies
    bot.handle(msg("/clear", 7))
    deleted = [p["message_ids"] for m, p in api.calls if m == "deleteMessages"]
    flat = [i for chunk in deleted for i in chunk]
    assert 5 in flat and 7 in flat and 1 in flat              # logged ones + sweep back to message #1
    assert len(db.chat_msgs(KID)) == 1                        # only the "🧹 cleaned" note remains
    assert "direct_submit" not in json.dumps(settings_view(db, "papaya01")[1])
    db.set_solver("papaya01", True)
    assert "direct_submit" in json.dumps(settings_view(db, "papaya01")[1])


def test_list_has_solve_all_button_only_with_grant():
    db = DB(":memory:"); db.set_chat("papaya01", KID)
    run_cycle(db, "papaya01", parse_assignments(A) + parse_exams(E), Notif(), T(4, 13))
    api = FakeApi(); bot = Bot(api, db, ADMIN, ["papaya01"], solver=types.SimpleNamespace())
    m = {"message": {"chat": {"id": KID}, "text": "/list", "from": {}, "message_id": 1}}
    bot.handle(m, T(4, 13, 5))
    assert "solveall" not in json.dumps(api.calls)
    db.set_solver("papaya01", True); bot.handle(m, T(4, 13, 5))
    assert "solveall" in json.dumps(api.calls)


# ---------------- v6: third model only for disputed questions, majority vote ----------------
def _ask3(a, b, c, log):
    def ask(m, msgs):
        log.append((m, [p["text"] for p in msgs[1]["content"] if p["type"] == "text"]))
        return reply({"a": a, "b": b, "c": c}[m]), {"a": 0.001, "b": 0.001, "c": 0.02}[m]
    return ask


def test_no_tiebreak_call_when_both_agree():
    log = []
    r = solve(QS, "", "", _ask3([2, 2], [2, 2], [1, 1], log), "a", "b", "c")
    assert [m for m, _ in log].count("c") == 0 and r.all_agree and r.cost == 0.002


def test_tiebreak_only_sees_disputed_question_and_decides_by_majority():
    log = []
    # q1 agreed (2), q2 disputed: a=2, b=1, c=1 -> majority 1
    r = solve(QS, "", "", _ask3([2, 2], [2, 1], [None, 1], log), "a", "b", "c")
    sent_to_c = next(t for m, t in log if m == "c")
    assert any("q2" in t for t in sent_to_c) and not any("[id=q1]" in t for t in sent_to_c)
    assert r.answers[1].choice == 1 and r.answers[1].agree and r.answers[1].tiebreak
    assert r.answers[0].agree and not r.answers[0].tiebreak and r.all_agree


def test_three_different_answers_stay_warning():
    r = solve([QS[0]], "", "", _ask3([1], [2], [3], []), "a", "b", "c")
    assert not r.answers[0].agree and not r.all_agree


def test_tiebreak_skipped_near_budget_cap():
    log = []
    r = solve(QS, "", "", _ask3([2, 2], [2, 1], [None, 1], log), "a", "b", "c", tiebreak_ok=lambda: False)
    assert "c" not in [m for m, _ in log] and not r.answers[1].agree


def test_service_stops_tiebreak_at_80_percent_of_budget():
    db, svc, n, ad, calls, iid = setup(answers_b=(2, 1))
    svc.models = ("google/gemini-x:online", "anthropic/haiku-x", "anthropic/sonnet-x")
    db.add_spend("2026-11", 8.5)                                 # 85% of 10$
    assert svc.prepare("papaya01", iid, KID) == "ready"
    assert "anthropic/sonnet-x" not in calls and "راجعها" in n.to(KID)[-1]


def test_preview_marks_majority_answers():
    db, svc, n, ad, calls, iid = setup(answers_b=(2, 1))
    svc.models = ("google/gemini-x:online", "anthropic/haiku-x", "anthropic/sonnet-x")
    base = svc.ask
    svc.ask = lambda m, msgs: (reply([None, 2]), 0.02) if "sonnet" in m else base(m, msgs)
    assert svc.prepare("papaya01", iid, KID) == "ready"
    assert "☑️" in n.to(KID)[-1] and "حسمها النموذج الثالث" in n.to(KID)[-1]


def test_list_message_is_redrawn_without_the_item_after_solve_and_submit(monkeypatch):
    freeze_bot_clock(monkeypatch)
    db, svc, n, ad, calls, _ = setup()
    db.set_setting("papaya01", "horizon_days", 0)          # whole list
    api = FakeApi()
    bot = Bot(api, db, ADMIN, ["papaya01"], solver=svc, spawn=lambda fn, *a: fn(*a))
    exam_id = listed_exam(bot)                               # really listed before the action
    msg = {"chat": {"id": KID}, "message_id": 9, "text": "الواجبات والاختبارات 📋"}
    cbq = lambda data: {"callback_query": {"id": "1", "data": data, "message": msg}}
    bot.handle(cbq(f"solve:{exam_id}"))                      # solves, shows preview
    bot.handle(cbq(f"submit:{exam_id}"))                     # submits
    edits = [p for m, p in api.calls if m == "editMessageText"]
    assert edits, "the list message must be redrawn after the job"
    assert exam_id not in json.dumps(edits[-1], ensure_ascii=False)
    assert ("exam", exam_id) in db.done_keys("papaya01")


def test_submitted_item_is_not_alerted_or_listed_by_the_cycle():
    db, svc, n, ad, calls, _ = setup()
    exam_id = next(i.id for i in parse_exams(E) if i.is_open and i.solve_url)
    svc.prepare("papaya01", exam_id, KID); svc.submit("papaya01", exam_id, KID)
    n2 = Notif(); run_cycle(db, "papaya01", parse_assignments(A) + parse_exams(E), n2, T(4, 16))
    assert not any(f":{exam_id}" in json.dumps(m, ensure_ascii=False) for m in n2.sent)


def test_item_shows_a_spinner_while_it_is_being_solved(tmp_path, monkeypatch):
    freeze_bot_clock(monkeypatch)
    import time
    path = str(tmp_path / "t.db")                              # a real file: the spinner thread opens its own connection
    db, svc, n, ad, calls, _ = setup(db=DB(path))
    db.set_setting("papaya01", "horizon_days", 0)
    api = FakeApi(); seen = []

    def slow_prepare(acct, item_id, chat):                    # stands in for the 1-2 minute AI work
        for _ in range(40):
            seen[:] = [json.dumps(p, ensure_ascii=False) for m, p in api.calls if m == "editMessageText"]
            if any("جاري الحل" in s for s in seen): return
            time.sleep(0.05)

    bot = Bot(api, db, ADMIN, ["papaya01"], solver=types.SimpleNamespace(prepare=slow_prepare, submit=None, drop=None),
              spawn=lambda fn, *a: fn(*a), db_factory=lambda: DB(path))
    exam_id = listed_exam(bot)
    msg = {"chat": {"id": KID}, "message_id": 9, "text": "الواجبات والاختبارات 📋"}
    bot.handle({"callback_query": {"id": "1", "data": f"solve:{exam_id}", "message": msg}})
    spin = next(s for s in seen if "جاري الحل" in s)
    assert "noop:-" in spin and f"solve:{exam_id}" not in spin     # that item's 🆘 is now a spinner
    last = json.dumps([p for m, p in api.calls if m == "editMessageText"][-1], ensure_ascii=False)
    assert "جاري" not in last and f"solve:{exam_id}" in last      # spinner gone, 🆘 back when it ends


def test_solve_all_only_touches_what_the_time_filter_shows():
    from datetime import timedelta
    db, svc, n, ad, calls, _ = setup()
    db.set_setting("papaya01", "horizon_days", 1)          # the student chose: only what ends within 24h
    now = svc.now()
    shown = {r["id"] for r in svc._listed_items(db, "papaya01")}
    every = {r["id"] for r in svc._open_items(db, "papaya01")}
    hidden = every - shown
    assert shown and hidden, "fixture must have both items inside and outside the window"
    svc.solve_all("papaya01", None, KID)
    assert all(datetime.fromisoformat(db.item("papaya01", k, i)["deadline"]) - now <= timedelta(hours=24)
               for i in shown for k in ["assignment" if db.item("papaya01", "assignment", i) else "exam"])
    assert all(db.solution("papaya01", i) is not None for i in shown)      # filtered ones were solved
    assert all(db.solution("papaya01", i) is None for i in hidden)         # hidden ones were left alone


def test_auto_mode_solves_both_assignments_and_exams_within_5h_and_reports(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    db, svc, n, ad, calls, _ = setup(now=T(5, 4, 0))
    db.set_setting("papaya01", "direct_submit", 1)                 # solve + submit in one go, then report
    exams = {i.id for i in parse_exams(E)}
    for _ in range(6):                                              # a few check cycles
        svc.auto("papaya01")
    sols = {r["item_id"]: r["status"] for r in db.con.execute("select item_id,status from solutions")}
    assert any(i in exams for i in sols), "an exam inside the 5h window must be handled"
    assert any(i not in exams for i in sols), "an assignment inside the 5h window must be handled"
    assert all(st == "submitted" for st in sols.values())
    report = [t for t in n.to(KID) if "حليته وسلّمته تلقائياً" in t]
    assert report and all("كان باقي" in t and "📚" in t for t in report)
    # nothing further than 5h away was touched
    from datetime import timedelta
    for i in sols:
        k = "exam" if i in exams else "assignment"
        assert datetime.fromisoformat(db.item("papaya01", k, i)["deadline"]) - T(5, 4, 0) <= timedelta(hours=5)


def test_vip_gets_auto_solve_on_and_can_switch_it_off_while_normal_users_have_no_solving(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    db = DB(":memory:")
    for a, chat in (("papaya01", KID), ("papaya02", 333)): db.set_chat(a, chat)
    db.set_setting("papaya01", "auto_solve", 0)                  # even if it had been off before
    db.set_solver("papaya01", True)                               # papaya01 becomes VIP, papaya02 stays normal
    assert db.get_settings("papaya01").auto_solve is True
    api = FakeApi(); ran = []
    solver = types.SimpleNamespace(prepare=lambda *a: ran.append(a), submit=lambda *a: ran.append(a),
                                   drop=lambda *a: ran.append(a), solve_all=lambda *a: ran.append(a),
                                   submit_all=lambda *a: ran.append(a))
    bot = Bot(api, db, ADMIN, ["papaya01", "papaya02"], solver=solver, spawn=lambda fn, *a: fn(*a))
    cb = lambda chat, data: {"callback_query": {"id": "1", "data": data,
                                                "message": {"chat": {"id": chat}, "message_id": 1}}}
    # VIP: sees the auto rows and can switch auto-solve off
    assert "t:auto_solve" in json.dumps(settings_view(db, "papaya01")[1])
    bot.handle(cb(KID, "t:auto_solve"))
    assert db.get_settings("papaya01").auto_solve is False
    # normal user: no solving rows, forged taps do nothing, nothing is ever run for them
    assert "auto_" not in json.dumps(settings_view(db, "papaya02")[1]) and "direct_submit" not in json.dumps(settings_view(db, "papaya02")[1])
    before = db.get_settings("papaya02")
    for data in ("t:auto_solve", "t:auto_submit", "t:direct_submit", "solve:X", "submit:X", "solveall:-", "submitall:-"):
        bot.handle(cb(333, data))
    assert ran == [] and db.get_settings("papaya02") == before
    # their alerts and list carry no 🆘 and no 'solve all' button
    n = Notif(); run_cycle(db, "papaya02", parse_assignments(A) + parse_exams(E), n, T(4, 13))
    assert "solve:" not in json.dumps(n.sent) and "solveall" not in json.dumps(bot.list_view("papaya02", T(4, 15))[1] or {})
    # and the engine itself refuses them even when called directly
    svc = SolveService(lambda: db, Notif(), lambda: ADMIN, lambda a: pathlib.Path("/tmp/x"), ask=lambda m, x: ("", 0.0),
                       page_runner=lambda acct, url, fn: fn(object()), adapter=Adapter(), now_fn=lambda: T(5, 4, 0))
    assert svc.auto("papaya02") == [] and svc.prepare("papaya02", "X", 333) == "denied"
    assert svc.solve_all("papaya02", None, 333) == "denied"


# ---- reading the page safely: retry only when it can help ------------------------------
from papaya import assignment_page as ap


class FlakyAdapter(Adapter):
    """Raises the given errors (or returns the given lists) one per call, then the questions."""
    def __init__(self, *steps):
        super().__init__(); self.steps = list(steps); self.calls = 0
    def read_questions(self, page, allow_exam=False):
        self.calls += 1
        if self.steps:
            s = self.steps.pop(0)
            if isinstance(s, Exception):
                raise s
            return s
        return QS


def flaky(*steps):
    db, svc, n, _, calls, iid = setup()
    ad = FlakyAdapter(*steps); svc.adapter = ad
    waits = []; svc.sleep = waits.append
    return db, svc, n, ad, waits, iid


def test_platform_error_is_retried_then_solved():
    db, svc, n, ad, waits, iid = flaky(ap.PageNotReady("error"), [])
    assert svc.prepare("papaya01", iid, KID) == "ready"
    assert ad.calls == 3 and waits == [20, 60]
    assert db.get_meta(f"qcount:{iid}") == "2"                  # remembered for next time


def test_finished_assignment_is_not_retried_and_student_is_told_why():
    db, svc, n, ad, waits, iid = flaky(ap.PageNotReady("done"))
    assert svc.prepare("papaya01", iid, KID).startswith("page_error")
    assert ad.calls == 1 and waits == []
    assert any("مسلَّم من قبل" in t for t in n.to(KID))


def test_layout_change_is_never_retried_or_solved():
    db, svc, n, ad, waits, iid = flaky(ap.LayoutChanged(1, 3))
    assert svc.prepare("papaya01", iid, KID).startswith("page_error")
    assert ad.calls == 1 and waits == [] and not db.solution("papaya01", iid)


def test_fewer_questions_than_last_time_is_refused():
    db, svc, n, ad, waits, iid = flaky()
    db.set_meta(f"qcount:{iid}", "3")                           # this assignment had 3 before
    assert svc.prepare("papaya01", iid, KID).startswith("page_error")
    assert ad.calls == 3 and not db.solution("papaya01", iid)
    assert any("قريت 2 سؤال من أصل 3" in t for t in n.to(KID))
