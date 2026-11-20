"""The web app API: login codes, what each phone may see and do, solving, settings, admin, chat, push mirror."""
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from papaya import web
from papaya.cycle import run_cycle
from papaya.db import DB
from papaya.parser import parse_assignments, parse_exams
from papaya.service import SolveService
from tests.test_parser import A, E
from tests.test_solver import ADMIN, KID, Adapter, Notif, T, reply

SECRET = "s" * 32
NOW = T(4, 15)


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "papaya.db")
    db = DB(path)
    for name, chat in (("papaya01", KID), ("papaya02", 333)):
        db.set_chat(name, chat)
        run_cycle(db, name, parse_assignments(A) + parse_exams(E), Notif(), T(4, 13))
    db.set_everyone(True)                 # homework for everyone, exams only by grant
    db.set_solver("papaya02", True)
    monkeypatch.setattr(web, "now_riyadh", lambda: NOW)
    pushed = []
    monkeypatch.setattr(web, "_webpush", lambda sub, data, v: pushed.append((sub["endpoint"], json.loads(data))))
    class Routed(Notif):                  # what web.install() does to the real Telegram notifier
        def send(self, chat, text, markup=None, silent=False):
            if isinstance(chat, str) and chat.startswith(web.WEB):
                return web.web_send(DB(path), chat[len(web.WEB):], text, markup, silent)
            return super().send(chat, text, markup, silent)

    n, ad = Routed(), Adapter()
    svc = SolveService(lambda: DB(path), n, lambda: ADMIN, lambda a: pathlib.Path("/tmp/x"),
                       ask=lambda m, msgs: (reply([2, 2]), 0.01), page_runner=lambda acct, url, fn: fn(object()),
                       adapter=ad, models=("google/gemini-x:online", "anthropic/haiku-x"), now_fn=lambda: NOW)
    app = web.App(path, solver=svc, secret=SECRET)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, token=None, secret=SECRET, headers=None):
        h = {"Content-Type": "application/json", "X-Papaya-Secret": secret, **(headers or {})}
        if token:
            h["Authorization"] = "Bearer " + token
        req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, h,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def log_in(name):
        code = web.new_code(DB(path), name)
        st, out = call("/api/login", {"code": code.lower()})          # case and spaces do not matter
        assert st == 200
        return out["token"]

    yield type("W", (), dict(path=path, call=staticmethod(call), login=staticmethod(log_in), pushed=pushed,
                             svc=svc, ad=ad, app=app))
    srv.shutdown()


def wait_job(w, token, item_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        st, out = w.call(f"/api/solution?id={item_id}", token=token)
        if out["job"] not in (None, "working"):
            return out
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_requests_without_the_proxy_secret_are_refused(world):
    assert world.call("/api/ping", secret="wrong")[0] == 403
    assert world.call("/api/ping")[0] == 200


def test_wrong_code_and_too_many_tries(world):
    for _ in range(web.LOGIN_TRIES):
        assert world.call("/api/login", {"code": "ZZZZZZ"})[0] == 401
    assert world.call("/api/login", {"code": "ZZZZZZ"})[0] == 429


def test_new_code_replaces_old_and_session_stays(world):
    db = DB(world.path)
    old = web.new_code(db, "papaya01")
    st, out = world.call("/api/login", {"code": old})
    token = out["token"]
    web.new_code(db, "papaya01")
    assert world.call("/api/login", {"code": old})[0] == 401          # old code is dead
    assert world.call("/api/state", token=token)[0] == 200            # the phone stays logged in
    assert world.call("/api/logout", {}, token=token)[0] == 200
    assert world.call("/api/state", token=token)[0] == 401


def test_state_is_the_bot_view_for_that_device(world):
    tok = world.login("papaya01")
    st, s = world.call("/api/state", token=tok)
    assert st == 200 and s["device"] == "papaya01" and not s["admin"]
    hw = [i for i in s["items"] if i["kind"] == "assignment"]
    ex = [i for i in s["items"] if i["kind"] == "exam"]
    assert s["counts"] == {"assignment": len(hw), "exam": len(ex)}
    assert any(i["can_solve"] for i in hw)                 # everyone switch: homework yes
    assert not any(i["can_solve"] for i in ex)             # no own grant: exams no
    assert "auto_solve" in s["settings"]
    tok2 = world.login("papaya02")
    s2 = world.call("/api/state", token=tok2)[1]
    assert any(i["can_solve"] for i in s2["items"] if i["kind"] == "exam") or not ex


def test_a_kid_cannot_see_another_device_or_admin(world):
    tok = world.login("papaya01")
    s = world.call("/api/state", token=tok, headers={"X-As": "papaya02"})[1]
    assert s["device"] == "papaya01"                       # X-As is ignored for non-admins
    assert world.call("/api/admin", token=tok)[0] == 403
    assert world.call("/api/admin/everyone", {"on": False}, token=tok)[0] == 403
    assert world.call("/api/admin/code", {"name": "papaya02"}, token=tok)[0] == 403


def test_solve_then_submit_from_the_web(world):
    tok = world.login("papaya01")
    item = next(i for i in world.call("/api/state", token=tok)[1]["items"] if i["can_solve"])
    assert world.call("/api/solve", {"id": item["id"]}, token=tok)[1]["job"] == "working"
    out = wait_job(world, tok, item["id"])
    assert out["job"] == "ready" and out["solution"]["status"] == "ready"
    assert [a["answer"] for a in out["solution"]["answers"]] == ["ب) 4", "ب) الرياض"]
    msgs = world.call("/api/inbox", token=tok)[1]["messages"]
    assert any(b.get("action") == "submit" for m in msgs for b in m.get("buttons", []))   # same buttons as Telegram
    world.call("/api/submit", {"id": item["id"]}, token=tok)
    assert wait_job(world, tok, item["id"])["job"] == "submitted" and world.ad.submitted == [2, 2]
    s = world.call("/api/state", token=tok)[1]
    assert item["id"] not in [i["id"] for i in s["items"]]        # submitted = gone from the list


def test_exam_solve_refused_without_grant(world):
    tok = world.login("papaya01")
    exam = next((i for i in world.call("/api/state", token=tok)[1]["items"] if i["kind"] == "exam"), None)
    if exam is None:
        pytest.skip("fixture has no upcoming exam")
    world.call("/api/solve", {"id": exam["id"]}, token=tok)
    assert wait_job(world, tok, exam["id"])["job"] == "invalid"


def test_switch_off_locks_solving(world):
    DB(world.path).set_everyone(False)
    tok = world.login("papaya01")
    s = world.call("/api/state", token=tok)[1]
    assert not any(i["can_solve"] for i in s["items"]) and "auto_solve" not in s["settings"]
    assert world.call("/api/solve", {"id": s["items"][0]["id"]}, token=tok)[0] == 403
    assert world.call("/api/settings", {"auto_submit": True}, token=tok)[0] == 403


def test_settings_validated(world):
    tok = world.login("papaya01")
    st, s = world.call("/api/settings", {"notify_new": False, "horizon_days": 7, "remind_hours": 6}, token=tok)
    assert st == 200 and s["notify_new"] is False and s["horizon_days"] == 7 and s["remind_hours"] == 6
    assert world.call("/api/settings", {"remind_hours": 99}, token=tok)[0] == 400
    assert world.call("/api/settings", {"chat_id": 1}, token=tok)[0] == 400


def test_admin_switches_and_codes(world):
    tok = world.login("@admin")
    st, s = world.call("/api/state", token=tok, headers={"X-As": "papaya02"})
    assert st == 200 and s["admin"] and s["device"] == "papaya02" and s["devices"] == ["papaya01", "papaya02"]
    a = world.call("/api/admin/everyone", {"on": False}, token=tok)[1]
    assert a["everyone"] is False and not DB(world.path).solver_enabled("papaya01")
    a = world.call("/api/admin/solver", {"name": "papaya01", "on": True, "hours": 2}, token=tok)[1]
    d = next(x for x in a["devices"] if x["name"] == "papaya01")
    assert d["granted"] and d["until"]
    code = world.call("/api/admin/code", {"name": "papaya01"}, token=tok)[1]["code"]
    assert world.call("/api/login", {"code": code})[0] == 200
    assert world.call("/api/state", token=tok, headers={"X-As": "nobody"})[0] == 400


def test_chat_understands_plain_arabic(world):
    tok = world.login("papaya01")
    out = world.call("/api/chat", {"text": "وش عندي؟"}, token=tok)[1]["messages"]
    assert out[0]["who"] == "me" and out[1]["who"] == "bot" and out[1].get("items")
    subj = out[1]["items"][0]["subject"]
    out = world.call("/api/chat", {"text": f"حل {subj}"}, token=tok)[1]["messages"]
    assert "أحل" in out[-1]["text"] or "ما أقدر" in out[-1]["text"]
    out = world.call("/api/chat", {"text": "قصيدة عن القطط"}, token=tok)[1]["messages"]
    assert out[-1].get("chips")


def test_bot_messages_reach_the_phone_as_push_and_web_chat(world, monkeypatch):
    tok = world.login("papaya01")
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
    assert world.call("/api/push", {"subscription": sub}, token=tok)[0] == 200
    from papaya import notifier as nmod
    monkeypatch.setattr(web, "_installed", False)
    real_send, real_delete = nmod.TelegramNotifier.send, nmod.TelegramNotifier.delete
    monkeypatch.setattr(nmod.TelegramNotifier, "send", lambda self, c, t, m=None, silent=False: 77)
    monkeypatch.setattr(nmod.TelegramNotifier, "delete", lambda self, c, ids: None)
    monkeypatch.setattr(web, "_bg", lambda fn, *a: fn(*a))
    web.install(world.path)
    n = nmod.TelegramNotifier("x")
    assert n.send(KID, "<b>واجب جديد</b> 📚") == 77                       # Telegram as before
    assert world.pushed[-1][1]["body"] == "واجب جديد 📚"                  # plus a push, HTML removed
    n.send(KID, "أحل… ⏳", silent=True)
    assert len(world.pushed) == 1                                         # progress: no push
    mid = n.send("web:papaya01", "خاص بالموقع")                           # web only
    assert isinstance(mid, int) and len(world.pushed) == 2
    texts = [m["text"] for m in world.call("/api/inbox", token=tok)[1]["messages"]]
    assert "واجب جديد 📚" in texts and "خاص بالموقع" in texts
    n.delete("web:papaya01", [mid])
    assert "خاص بالموقع" not in [m["text"] for m in world.call("/api/inbox", token=tok)[1]["messages"]]


def test_dead_push_subscription_is_removed(world, monkeypatch):
    db = DB(world.path)
    web.subscribe(db, "papaya01", {"endpoint": "https://push.example/gone", "keys": {"p256dh": "x", "auth": "y"}})

    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def boom(sub, data, v):
        raise Gone()
    monkeypatch.setattr(web, "_webpush", boom)
    assert web.push(db, "papaya01", "hi") == 0 and web.subs(db, "papaya01") == []


def test_queue_from_while_asleep_runs_on_wake(world):
    tok = world.login("papaya01")
    item = next(i for i in world.call("/api/state", token=tok)[1]["items"] if i["can_solve"])
    web.subscribe(DB(world.path), "papaya01", {"endpoint": "https://push.example/q", "keys": {"p256dh": "x", "auth": "y"}})
    import os
    os.environ["PAPAYA_WEB_QUEUE_URL"] = "https://example.invalid/q"
    try:
        n = web.take_queue(world.app, fetch=lambda: [{"th": web._h(tok), "action": "solve", "id": item["id"]},
                                                     {"th": web._h("stolen"), "action": "solve", "id": item["id"]},
                                                     {"th": web._h(tok), "action": "notify"}])
    finally:
        del os.environ["PAPAYA_WEB_QUEUE_URL"]
    assert n == 2                                             # the unknown token is ignored
    assert wait_job(world, tok, item["id"])["job"] == "ready"
    assert any("صحي" in p[1]["body"] for p in world.pushed)


def test_command_line_is_the_real_bot(world, monkeypatch):
    monkeypatch.setenv("PAPAYA_ADMIN_CHAT_ID", str(ADMIN))
    tok = world.login("papaya01")
    out = world.call("/api/cmd", {"text": "/settings"}, token=tok)[1]
    me, reply_ = out["messages"][0], out["messages"][-1]
    assert me["me"] and me["text"] == "/settings"
    assert "إعدادات" in reply_["text"] and reply_["buttons"]               # the same inline keyboard as Telegram
    tap = next(b for row in reply_["buttons"] for b in row if b.get("data", "").startswith("r:"))
    v = out["v"]
    out = world.call("/api/cmd", {"data": tap["data"], "id": reply_["id"], "after": v}, token=tok)[1]
    assert out["messages"] and out["messages"][0]["id"] == reply_["id"]    # the message was edited in place
    assert DB(world.path).get_settings("papaya01").remind_hours == float(tap["data"][2:]) and out["v"] > v
    out = world.call("/api/cmd", {"text": "/admin"}, token=tok)[1]          # not the admin: no admin panel
    assert "الإدارة" not in out["messages"][-1]["text"]
    out = world.call("/api/cmd", {"text": "/clear"}, token=tok)[1]
    assert "تيليجرام بس" in out["messages"][-1]["text"]
    assert all(m["id"] > web.FAKE_MID for m in out["messages"])            # never a real Telegram message id
    out = world.call("/api/cmd", {"text": "cls"}, token=tok)[1]
    assert out["gone"] and not world.call("/api/cmd?after=0", token=tok)[1]["messages"]


def test_admin_command_line(world, monkeypatch):
    monkeypatch.setenv("PAPAYA_ADMIN_CHAT_ID", str(ADMIN))
    tok = world.login("@admin")
    out = world.call("/api/cmd", {"text": "/everyone off"}, token=tok)[1]
    assert "مطفي" in out["messages"][-1]["text"] and not DB(world.path).everyone_solves()
    out = world.call("/api/cmd", {"text": "/admin"}, token=tok)[1]
    assert out["messages"][-1]["buttons"]


def test_live_status_message_on_the_web(world, monkeypatch):
    """The owner's solve-all progress message is edited in place; on the web it updates one chat line."""
    tok = world.login("papaya01")
    from papaya import notifier as nmod
    monkeypatch.setattr(web, "_installed", False)
    monkeypatch.setattr(nmod.TelegramNotifier, "send", lambda self, c, t, m=None, silent=False: 5)
    monkeypatch.setattr(nmod.TelegramNotifier, "delete", lambda self, c, ids: None)
    monkeypatch.setattr(nmod.TelegramNotifier, "edit", lambda self, c, mid, t, m=None: True, raising=False)
    monkeypatch.setattr(web, "_bg", lambda fn, *a: fn(*a))
    web.subscribe(DB(world.path), "papaya01", {"endpoint": "https://push.example/l", "keys": {"p256dh": "x", "auth": "y"}})
    web.install(world.path)
    n = nmod.TelegramNotifier("x")
    mid = n.send("web:papaya01", "<b>أحل…</b> ⏳ 0/3", silent=True)
    n.edit("web:papaya01", mid, "<b>أحل…</b> ⏳ 2/3")
    assert not world.pushed
    n.edit("web:papaya01", mid, "<b>الحلول جاهزة</b> 🆘", {"inline_keyboard": [[{"text": "سلّم الكل", "callback_data": "submitall:-"}]]})
    msgs = [m for m in world.call("/api/inbox", token=tok)[1]["messages"] if m["id"] == mid]
    assert msgs[0]["text"] == "الحلول جاهزة 🆘" and msgs[0]["buttons"][0]["action"] == "submitall"
    assert world.pushed and world.pushed[-1][1]["body"] == "الحلول جاهزة 🆘"
    n.edit(KID, 9, "تم ✅")                                                # a real chat's final edit is pushed too
    assert world.pushed[-1][1]["body"] == "تم ✅"
