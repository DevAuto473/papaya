"""Solve flow: permission -> budget -> read page -> two models -> preview -> (tap) submit.

Everything that talks to Telegram/OpenRouter/the browser is injected, so tests run offline."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from types import SimpleNamespace

from . import ai, assignment_page, scraper, solver
from .parser import RIYADH
from .reminders import fmt_left, in_horizon, in_quiet_hours

AUTO_SOLVE_BEFORE = timedelta(hours=5)
AUTO_SUBMIT_BEFORE = timedelta(hours=1)
AUTO_PER_PASS = 3          # items handled per check (every ~10 min), earliest deadline first
MAX_MSG = 3500
RETRY_DELAYS = (20, 60)    # seconds to wait before the 2nd and 3rd try of a page that may come back


def budget_usd() -> float:
    try:
        return float(os.environ.get("PAPAYA_MONTHLY_BUDGET", "10"))
    except ValueError:
        return 10.0


def _short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _opt(q: dict, n: int | None) -> str:
    if n is None:
        return "؟"
    opts = q.get("options") or []
    label = opts[n - 1] if 0 < n <= len(opts) else ""
    return f"({n}) {_short(label, 60)}"


def render_preview(title: str, subject: str, left: str, data: dict, auto: bool) -> list[str]:
    qs = {q["id"]: q for q in data["questions"]}
    ans = data["answers"]
    ok = sum(a["agree"] for a in ans)
    head = ("<b>حليته لك تلقائياً</b> — باقي " + left + " 🤖" if auto else "<b>الحل جاهز</b> — باقي " + left + " 🆘")
    head += f"\n<b>{escape(title)}</b>\n{escape(subject)} 📚\n"
    tb = sum(bool(a.get("tiebreak")) for a in ans)
    head += (f"كل الإجابات ({ok}/{len(ans)}) متأكد منها ✅" if ok == len(ans)
             else f"{ok} من {len(ans)} متأكد منها ✅ · {len(ans) - ok} راجعها ⚠️")
    if tb:
        head += f"\n{tb} حسمها النموذج الثالث بالأغلبية ☑️"
    blocks = []
    for n, a in enumerate(ans, 1):
        q = qs.get(a["qid"], {"text": "", "options": [], "kind": "choice"})
        b = f"\n\n<b>{n})</b> {escape(_short(q['text'], 90))}\n"
        mark = ("☑️" if a.get("tiebreak") else "✅") if a["agree"] else "⚠️"
        if q.get("kind") == "choice":
            b += f"{escape(_opt(q, a['choice']))} {mark}"
            if not a["agree"] and a.get("alt_choice") is not None:
                b += f"\n   النموذج الثاني: {escape(_opt(q, a['alt_choice']))}"
        elif q.get("kind") == "match" and a.get("seq"):
            b += "مزاوجة: " + mark + "".join(
                f"\n   • {escape(_short(p, 35))} ← {escape(_opt(q, x))}" for p, x in zip(q.get("prompts", []), a["seq"]))
            if not a["agree"] and a.get("alt_seq"):
                b += f"\n   النموذج الثاني: {'، '.join(map(str, a['alt_seq']))}"
        elif q.get("kind") == "order" and a.get("seq"):
            b += "الترتيب: " + mark + "".join(f"\n   {i}. {escape(_short(q['options'][x - 1], 60))}"
                                             for i, x in enumerate(a["seq"], 1))
            if not a["agree"] and a.get("alt_seq"):
                b += f"\n   النموذج الثاني: {'، '.join(map(str, a['alt_seq']))}"
        elif q.get("kind") == "unknown" or (a.get("choice") is None and not a.get("seq") and not a.get("text")):
            b += "حلّه بنفسك ⚠️"
        else:
            b += f"{escape(_short(a.get('text') or '؟', 300))} ✍️ {'✅' if a['agree'] else '⚠️'}"
        if a.get("why"):
            b += f"\n{escape(_short(a['why'], 140))} 💡"
        blocks.append(b)
    pages, cur = [], head
    for b in blocks:
        if len(cur) + len(b) > MAX_MSG:
            pages.append(cur); cur = "…" + b
        else:
            cur += b
    pages.append(cur)
    return pages


def preview_markup(item_id: str, url: str | None, all_agree: bool) -> dict:
    rows = [[{"text": "سلّم الإجابات 📤" if all_agree else "سلّم (فيه راجعها أول ⚠️) 📤",
              "callback_data": f"submit:{item_id}"}]]
    if url:
        rows.append([{"text": "افتح الواجب 🔗", "url": url}])
    rows.append([{"text": "تجاهل ❌", "callback_data": f"drop:{item_id}"}])
    return {"inline_keyboard": rows}


class SolveService:
    def __init__(self, db_factory, notifier, admin_id_fn, profile_dir_fn,
                 ask=None, page_runner=None, adapter=assignment_page, models=None, now_fn=None,
                 verify_fn=None, on_change=None, confirm_after=120, retry_delays=RETRY_DELAYS, sleep=None):
        self.db_factory = db_factory
        self.notifier = notifier
        self.admin_id_fn = admin_id_fn
        self.profile_dir_fn = profile_dir_fn
        self._real_ai = ask is None
        self.ask = ask or (lambda m, msgs: ai.chat(m, msgs))
        self.page_runner = page_runner or self._open_page
        self.retry_delays = tuple(retry_delays)
        import time
        self.sleep = sleep or time.sleep
        self.adapter = adapter
        self.models = models or ai.models()
        self.now = now_fn or (lambda: datetime.now(RIYADH))
        # verify_fn(acct, item_id) -> True (no longer open = submitted) / False (still open) / None (unknown)
        self.verify_fn = verify_fn
        self.on_change = on_change          # on_change(acct): redraw that phone's 📋 list
        self.confirm_after = confirm_after  # seconds; look at the list again after a submit (None = no)
        self.spawn_bg = lambda fn: threading.Thread(target=fn, daemon=True).start()
        self._busy: set = set()
        self._lock = threading.Lock()
        self._progress: dict = {}          # chat -> ids of "⏳ ..." messages to remove when done

    def _open_page(self, acct, url, fn):
        """Real browser. Passes the account's assignments list so an ended session logs in again."""
        try:
            urls = json.loads(self.db_factory().get_meta(f"urls:{acct}") or "{}")
        except Exception:
            urls = {}
        return scraper.with_page(self.profile_dir_fn(acct), url, fn, login_url=urls.get("assign"))

    def _read_page(self, db, acct, row, chat, quiet_progress: bool):
        """Read the questions, trying again (with a pause) only when that can help: a platform error,
        no internet, a browser hiccup, a page without questions, or fewer questions than this same
        assignment had before. Never retries an exam guard, a layout change or a finished assignment."""
        fn = self._page_fn(row, "read_questions")
        key = f"qcount:{row['id']}"
        try:
            known = int(db.get_meta(key) or 0)
        except ValueError:
            known = 0
        tries = len(self.retry_delays) + 1
        for i in range(tries):
            last = i == tries - 1
            why = ""
            try:
                qs = self.page_runner(acct, row["solve_url"], fn)
            except assignment_page.PageNotReady as e:
                if not e.retry or last:
                    raise
                why = e.en
            except (assignment_page.LayoutChanged, assignment_page.NotAllowed):
                raise
            except scraper.ScrapeError as e:          # browser hiccup, NetworkDown, ...
                if last:
                    raise
                why = e.en
            else:
                if not qs:
                    if last:
                        return qs
                    why = "no questions"
                elif known and len(qs) < known:       # the same assignment had more questions before
                    if last:
                        raise assignment_page.LayoutChanged(len(qs), known)
                    why = f"{len(qs)} questions, {known} before"
                else:
                    if len(qs) != known:
                        db.set_meta(key, str(len(qs)))
                    return qs
            wait = self.retry_delays[i]
            db.log(acct, f"قراءة الواجب: محاولة {i + 2} بعد {wait} ثانية ({why}) 🔁")
            if not quiet_progress:
                self._say_progress(chat, "صفحة الواجب ما فتحت زين، أحاول مرة ثانية… ⏳")
            self.sleep(wait)
        return []

    # ---- helpers -------------------------------------------------------
    def _say(self, chat, text, markup=None, silent=False):
        return self.notifier.send(chat, text, markup, silent=silent)

    def _say_progress(self, chat, text):
        mid = self._say(chat, text, silent=True)
        if isinstance(mid, int) and not isinstance(mid, bool):
            with self._lock:
                self._progress.setdefault(chat, []).append(mid)

    def _clear_progress(self, chat):
        with self._lock:
            ids = self._progress.pop(chat, [])
        if ids and hasattr(self.notifier, "delete"):
            self.notifier.delete(chat, ids)

    def _admin(self, text):
        aid = self.admin_id_fn()
        if aid:
            self.notifier.send(aid, text, None, silent=True)

    def _claim(self, key) -> bool:
        """One job per assignment (and per 'solve all') at a time: double taps are ignored."""
        with self._lock:
            if key in self._busy:
                return False
            self._busy.add(key)
            return True

    def _release(self, key):
        with self._lock:
            self._busy.discard(key)

    def _check_item(self, db, acct, item_id):
        """Returns (row, reason_ar). Only open, on-platform assignments with time left."""
        row = db.item(acct, "assignment", item_id) or db.item(acct, "exam", item_id)
        if row is None:
            return None, "ما لقيت هذا الواجب (ممكن انحذف أو انسلّم)."
        if datetime.fromisoformat(row["deadline"]) <= self.now():
            return None, "انتهى وقت هذا الواجب."
        if (row["status"] or "").strip() == "منتظر":
            return None, "هذا الواجب لسه ما انفتح."
        if "قنوات أخرى" in (row["status"] or "") or not row["solve_url"]:
            return None, "هذا الواجب يُسلَّم خارج المنصة، ما أقدر أحله."
        return row, ""

    def _open_items(self, db, acct):
        now = self.now()
        rows = sorted(db.upcoming(acct, now, limit=60), key=lambda r: r["deadline"])
        return [r for r in rows if self._check_item(db, acct, r["id"])[0] is not None]

    def _listed_items(self, db, acct):
        """Exactly what the student sees in /list: open, not done, and inside their chosen time filter.
        'Solve all' / 'submit all' act on these only, never on items the filter hides."""
        now, horizon = self.now(), db.get_settings(acct).horizon_days
        done = db.done_keys(acct)
        return [r for r in self._open_items(db, acct)
                if (r["kind"], r["id"]) not in done
                and in_horizon(SimpleNamespace(deadline=datetime.fromisoformat(r["deadline"])), now, horizon)]

    def _page_fn(self, row, name: str):
        """The adapter function to run on the page; exams (homework-style) are allowed explicitly."""
        fn = getattr(self.adapter, name)
        if row["kind"] == "exam":
            return lambda page, *a: fn(page, *a, allow_exam=True)
        return fn

    # ---- solving (core, no permission/claim handling) --------------------
    def _solve_core(self, db, acct, row, chat, quiet_progress: bool):
        """Returns (status, data, all_agree). Uses a stored solution when there is one."""
        item_id, now = row["id"], self.now()
        sol = db.solution(acct, item_id)
        if sol and sol["status"] in ("ready", "failed"):
            return "cached", json.loads(sol["data"]), bool(sol["all_agree"])
        if sol and sol["status"] == "submitted":
            return "submitted", None, True
        month = now.strftime("%Y-%m")
        if db.spend(month) >= budget_usd():
            self._admin(f"سقف الحل الشهري ({budget_usd():g}$) انتهى — طلب من {acct} ⛔")
            return "budget", None, False
        if self._real_ai and not os.environ.get("OPENROUTER_API_KEY"):
            self._admin("ضع OPENROUTER_API_KEY في .env لتشغيل الحل ⚠️")
            return "nokey", None, False
        if not quiet_progress:
            self._say_progress(chat, f"أحل لك «{escape(_short(row['title'], 60))}»… (دقيقة أو دقيقتين) ⏳")
        try:
            questions = self._read_page(db, acct, row, chat, quiet_progress)
        except scraper.PapayaError as e:
            self._admin(f"حل {acct}: {e.ar} ⚠️")
            return "page_error:" + e.ar, None, False
        if not questions:
            self._admin(f"حل {acct}: «{escape(_short(row['title'], 60))}» بدون أسئلة بعد الانتظار وإعادة "
                        f"التحميل، صورة الصفحة في diagnose/ ⚠️")
            return "empty", None, False
        # the tie-breaker is the expensive model: stop using it near the cap so the cap is never crossed
        tiebreak_ok = lambda: db.spend(month) < 0.8 * budget_usd()
        used = [0]
        lock = threading.Lock()
        def ask(m, msgs):                                  # same call, but counts the tokens
            text, c = self.ask(m, msgs)
            with lock:
                used[0] += getattr(text, "tokens", 0)
            return text, c
        res = solver.solve(questions, row["subject"] or "", row["title"] or "", ask, *self.models,
                           tiebreak_ok=tiebreak_ok)
        db.add_spend(month, res.cost)
        if not any(a.choice is not None or a.text or a.seq for a in res.answers):
            self._admin(f"حل {acct} فشل: {'; '.join(res.errors)[:300]} ⚠️")
            return "ai_error", None, False
        data = {"questions": [asdict(q) for q in questions], **res.to_dict()}
        db.save_solution(acct, item_id, json.dumps(data, ensure_ascii=False), res.all_agree, now)
        ok = sum(a.agree for a in res.answers)
        st = db.add_ai_stats(acct, month, used[0], res.cost)
        db.log(acct, f"حل بالـ AI: {_short(row['title'], 50)} — {ok}/{len(res.answers)} ✅ — "
                     f"{used[0]:,} توكن — {res.cost:.3f}$ (الشهر: {st['solved']} واجب · {st['tokens']:,} توكن) 🆘")
        self._admin(f"{acct} حل: {_short(row['title'], 50)} — {ok}/{len(res.answers)} ✅ — "
                    f"{res.cost:.3f}$ (الشهر: {db.spend(month):.2f}$) 🆘")
        return "ready", data, res.all_agree

    def _submit_core(self, db, acct, row, data) -> tuple[str, str]:
        """Returns (status, reason_ar). Success is only claimed when the platform or the
        assignment list confirms it."""
        item_id = row["id"]
        qs = [solver.Question(**q) for q in data["questions"]]
        ans = [solver.Answer(**a) for a in data["answers"]]
        try:
            fn = self._page_fn(row, "fill_and_submit")
            self.page_runner(acct, row["solve_url"], lambda page: fn(page, qs, ans))
        except scraper.PapayaError as e:
            if isinstance(e, assignment_page.Unconfirmed) and self.verify_fn:
                gone = self.verify_fn(acct, item_id)        # check the assignments list instead
                if gone:
                    db.set_solution_status(acct, item_id, "submitted", self.now())
                    db.log(acct, f"سلّم: {row['title']} 📤")
                    return "submitted", ""
            db.set_solution_status(acct, item_id, "failed", self.now())
            db.log(acct, f"فشل التسليم: {row['title']} — {e.ar} ❌")
            return "failed", e.ar
        db.set_solution_status(acct, item_id, "submitted", self.now())
        db.log(acct, f"سلّم: {row['title']} 📤")
        self._confirm_later(acct, dict(row))
        return "submitted", ""

    # ---- one assignment ----------------------------------------------------

    def _confirm_later(self, acct, row):
        """The page said 'saved'. A bit later, check the assignments list itself: only then is it
        really done. If it is still open there, the student is told and it shows up again."""
        if not self.verify_fn or self.confirm_after is None:
            return
        def run():
            import time
            for wait in (self.confirm_after, self.confirm_after * 3):
                time.sleep(wait)
                gone = self.verify_fn(acct, row["id"])
                if gone is None:
                    continue                      # could not look (no internet...): try once more
                db = self.db_factory()
                if gone:
                    getattr(db, "log", lambda *a: None)(acct, f"✅ تأكدت من المنصة: {_short(row['title'], 50)} انسلّم")
                else:
                    db.set_solution_status(acct, row["id"], "failed", self.now())
                    getattr(db, "log", lambda *a: None)(acct, f"⚠️ بعد التسليم المنصة لسه تعرض {_short(row['title'], 50)} مفتوح")
                    chat = db.chat_id(acct)
                    if chat:
                        kb = {"inline_keyboard": [[{"text": "✍️ افتح الواجب", "url": row["solve_url"]}]]} \
                            if row.get("solve_url") else None
                        self._say(chat, f"⚠️ ضغطت تسليم على «{escape(row['title'])}» بس المنصة لسه تعرضه مفتوح.\n"
                                        "افتحه وتأكد بنفسك 👇", kb)
                if self.on_change:
                    try:
                        self.on_change(acct)
                    except Exception:
                        pass
                return
        self.spawn_bg(run)

    def prepare(self, acct: str, item_id: str, chat, auto: bool = False) -> str:
        db = self.db_factory()
        if not db.solver_enabled(acct):
            return "denied"                       # silently: the feature does not exist for them
        if not self._claim((acct, item_id)):
            return "busy"
        try:
            row, why = self._check_item(db, acct, item_id)
            if not row:
                self._say(chat, f"{why} ⚠️"); return "invalid"
            status, data, all_agree = self._solve_core(db, acct, row, chat, quiet_progress=auto)
            if status == "submitted":
                self._say(chat, "هذا الواجب انسلّم من قبل ✅"); return "submitted"
            if data is None:
                self._say(chat, _fail_text(status)); return status.split(":")[0]
            s = db.get_settings(acct)
            if s.direct_submit:                   # owner chose: no preview tap
                st, why = self._submit_core(db, acct, row, data)
                self._report_submit(acct, chat, row, data, st, why, auto)
                return st
            left = fmt_left(datetime.fromisoformat(row["deadline"]) - self.now())
            pages = render_preview(row["title"], row["subject"], left, data, auto)
            quiet = in_quiet_hours(self.now(), s)
            for p in pages[:-1]:
                self._say(chat, p, silent=quiet)
            self._say(chat, pages[-1], preview_markup(item_id, row["solve_url"], all_agree), silent=quiet)
            return "resent" if status == "cached" else "ready"
        finally:
            self._release((acct, item_id))
            self._clear_progress(chat)

    def submit(self, acct: str, item_id: str, chat, auto: bool = False) -> str:
        db = self.db_factory()
        if not db.solver_enabled(acct):
            return "denied"
        if not self._claim((acct, item_id)):
            return "busy"
        try:
            row, why = self._check_item(db, acct, item_id)
            if not row:
                self._say(chat, f"{why} ⚠️"); return "invalid"
            sol = db.solution(acct, item_id)
            if not sol or sol["status"] not in ("ready", "failed"):
                self._say(chat, "ما عندي حل جاهز لهذا الواجب. اضغط «حلّه لي» أول ⚠️" if not sol
                          else "هذا الواجب انسلّم من قبل ✅")
                return "nosolution"
            if not auto:
                self._say_progress(chat, "أسلّم الإجابات… ⏳")
            st, why = self._submit_core(db, acct, row, json.loads(sol["data"]))
            self._report_submit(acct, chat, row, json.loads(sol["data"]), st, why, auto)
            return st
        finally:
            self._release((acct, item_id))
            self._clear_progress(chat)

    def _report_submit(self, acct, chat, row, data, st, why, auto):
        ok = sum(a["agree"] for a in data["answers"])
        n = len(data["answers"])
        if st == "submitted":
            if auto:                  # the report: what it did, on which item, and how sure it is
                left = fmt_left(max(datetime.fromisoformat(row["deadline"]) - self.now(), timedelta(0)))
                kind = "اختبار" if row["kind"] == "exam" else "واجب"
                self._say(chat, f"<b>حليته وسلّمته تلقائياً</b> 🤖\n<b>{escape(row['title'])}</b>\n"
                          f"{escape(row['subject'] or '')} · {kind} 📚\nكان باقي {left} على الموعد ⏰\n"
                          + (f"كل الإجابات ({n}/{n}) اتفق عليها النموذجان ✅" if ok == n
                             else f"{ok} من {n} متأكد منها ✅ · {n - ok} ما اتفق عليها النموذجان ⚠️"))
            else:
                self._say(chat, f"تم التسليم: <b>{escape(row['title'])}</b> ✅"
                          + (f"\n{n - ok} من {n} ما اتفق عليها النموذجان ⚠️" if ok < n else ""))
            self._admin(f"{acct} {'(تلقائي) ' if auto else ''}سلّم: {_short(row['title'], 50)} ({ok}/{n} ✅) 📤")
        else:
            self._say(chat, f"ما انسلّم: {why} ❌\nافتح الواجب وسلّمه بنفسك.",
                      {"inline_keyboard": [[{"text": "افتح الواجب 🔗", "url": row["solve_url"]}]]})
            self._admin(f"تسليم {acct} فشل: {why} ❌")

    def drop(self, acct: str, item_id: str, chat) -> str:
        db = self.db_factory()
        if not db.solver_enabled(acct):
            return "denied"
        if db.solution(acct, item_id):
            db.set_solution_status(acct, item_id, "dropped", self.now())
        self._say(chat, "تمام، تجاهلت الحل. تقدر تطلبه مرة ثانية من «حلّه لي».")
        return "dropped"

    # ---- all assignments at once -------------------------------------------
    def solve_all(self, acct: str, _arg, chat) -> str:
        db = self.db_factory()
        if not db.solver_enabled(acct):
            return "denied"
        if not self._claim((acct, "*")):
            return "busy"
        try:
            rows = [r for r in self._listed_items(db, acct)
                    if not (db.solution(acct, r["id"]) and db.solution(acct, r["id"])["status"] == "submitted")]
            if not rows:
                done = db.done_keys(acct)
                exams = sum(1 for r in db.upcoming(acct, self.now(), limit=60)
                            if r["kind"] == "exam" and (r["kind"], r["id"]) not in done)
                self._say(chat, "ما عندك واجبات مفتوحة على المنصة 🎉"
                          + (f"\nباقي {exams} اختبار، والاختبارات تحلها بنفسك ✍️" if exams else ""))
                return "none"
            direct = db.get_settings(acct).direct_submit
            self._say_progress(chat, f"أحل {len(rows)} واجب{'ات' if len(rows) > 2 else ''}"
                            f"{' وأسلّمها' if direct else ''}… (تقريباً دقيقة لكل واجب) ⏳")
            lines, ready = [], 0
            for r in rows:
                if not self._claim((acct, r["id"])):
                    lines.append(f"{escape(_short(r['title'], 45))} — شغال عليه من قبل ⏳"); continue
                try:
                    status, data, agree = self._solve_core(db, acct, r, chat, quiet_progress=True)
                    if data is None:
                        lines.append(f"{escape(_short(r['title'], 45))} — {_fail_text(status, short=True)} ❌")
                        if status == "budget":
                            break
                        continue
                    ok, n = sum(a["agree"] for a in data["answers"]), len(data["answers"])
                    tag = f"{ok}/{n} ✅" if ok == n else f"{ok}/{n} ✅ · ⚠️ {n - ok}"
                    if direct:
                        st, why = self._submit_core(db, acct, r, data)
                        lines.append((f"{escape(_short(r['title'], 45))} — {tag} 📤" if st == "submitted"
                                      else f"{escape(_short(r['title'], 45))} — ما انسلّم: {escape(why)} ❌"))
                    else:
                        ready += 1
                        lines.append(f"🆗 {escape(_short(r['title'], 45))} — {tag}")
                finally:
                    self._release((acct, r["id"]))
            head = "<b>النتيجة</b> 📤" if direct else "<b>الحلول جاهزة</b> 🆘"
            kb = None
            if ready:
                kb = {"inline_keyboard": [[{"text": f"سلّم الكل ({ready}) 📤", "callback_data": "submitall:-"}]]}
                lines.append("\nتبغى تشوف حل واجب معيّن؟ اضغط 🆘 جنبه في /list")
            self._say(chat, head + "\n" + "\n".join(lines), kb)
            self._admin(f"{acct}: حل الكل — {len(rows)} واجب 🆘")
            return "done"
        finally:
            self._release((acct, "*"))
            self._clear_progress(chat)

    def submit_all(self, acct: str, _arg, chat) -> str:
        db = self.db_factory()
        if not db.solver_enabled(acct):
            return "denied"
        if not self._claim((acct, "*")):
            return "busy"
        try:
            todo = [(r, db.solution(acct, r["id"])) for r in self._listed_items(db, acct)]
            todo = [(r, s) for r, s in todo if s and s["status"] in ("ready", "failed")]
            if not todo:
                self._say(chat, "ما عندي حلول جاهزة للتسليم."); return "none"
            self._say_progress(chat, f"أسلّم {len(todo)} واجب… ⏳")
            lines = []
            for r, s in todo:
                if not self._claim((acct, r["id"])):
                    continue
                try:
                    st, why = self._submit_core(db, acct, r, json.loads(s["data"]))
                finally:
                    self._release((acct, r["id"]))
                lines.append(f"{escape(_short(r['title'], 45))} 📤" if st == "submitted"
                             else f"{escape(_short(r['title'], 45))} — {escape(why)} ❌")
            self._say(chat, "<b>التسليم</b> 📤\n" + "\n".join(lines))
            self._admin(f"{acct}: سلّم الكل — {sum(l.endswith('📤') for l in lines)}/{len(lines)} 📤")
            return "done"
        finally:
            self._release((acct, "*"))
            self._clear_progress(chat)

    # ---- automatic (called after each periodic check) --------------------
    def auto(self, acct: str) -> list[str]:
        db = self.db_factory()
        if not db.solver_enabled(acct) or (self._real_ai and not os.environ.get("OPENROUTER_API_KEY")):
            return []
        chat = db.chat_id(acct)
        if chat is None:
            return []
        s, now, done = db.get_settings(acct), self.now(), []
        for r in self._open_items(db, acct):
            left = datetime.fromisoformat(r["deadline"]) - now
            if left > AUTO_SOLVE_BEFORE:
                continue
            sol = db.solution(acct, r["id"])
            if sol is None and s.auto_solve:
                done.append(self.prepare(acct, r["id"], chat, auto=True))
            elif (sol and sol["status"] == "ready" and sol["all_agree"] and s.auto_submit
                    and left <= AUTO_SUBMIT_BEFORE):
                done.append(self.submit(acct, r["id"], chat, auto=True))
            else:
                continue
            if len(done) >= AUTO_PER_PASS:         # earliest deadlines first; the rest wait for the next check
                break                              # (bounds cost and keeps other accounts' checks on time)
        return done


def _fail_text(status: str, short: bool = False) -> str:
    kind, _, detail = status.partition(":")
    txt = {
        "budget": "وصلنا سقف المصروف الشهري للحل. يرجع الشهر الجاي أو يرفعه المشرف ⛔",
        "nokey": "ميزة الحل غير مفعّلة على السيرفر حالياً ⚠️",
        "page_error": f"ما قدرت أفتح الواجب: {detail} ⚠️",
        "empty": "ما لقيت أسئلة في صفحة الواجب ⚠️",
        "ai_error": "النماذج ما ردت بحل صالح. جرّب مرة ثانية بعد شوي ⚠️",
    }.get(kind, "صار خطأ ⚠️")
    return txt.split(".")[0] if short else txt
