"""Reads questions from a Madrasati solve page and fills/submits answers (ASSIGNMENTS ONLY).

Layout learned from a saved solve page. Each question is `div.dqc` with hidden `.qid` and
`.qidtype`:  0 = multiple choice (radio), 2 = true/false (radio), 3 = matching (one <select> per
item of list (أ)), 6 = ordering (sortable <li data-id>). Anything else is reported as
"unknown" and left for the student. Exam pages are refused in code, not only by the UI."""
from __future__ import annotations

import base64
import re

from bs4 import BeautifulSoup

from .scraper import MS_HOSTS, ScrapeError
from .solver import Answer, Question

TYPES = {"0": "choice", "2": "choice", "3": "match", "6": "order"}


class Unconfirmed(ScrapeError):
    """Submit was pressed but the page showed no clear success. The caller verifies another way."""
    def __init__(self):
        super().__init__("ضغطت تسليم لكن ما تأكدت أنه انحفظ، افتح الواجب وتأكد",
                         "submit pressed but no confirmation seen")


SCREENSHOT_DIR = None     # set by main: where to keep a picture of the page after submitting


class NotAllowed(ScrapeError):
    def __init__(self):
        super().__init__("هذي صفحة اختبار، والحل للواجبات فقط", "exam page: solving is for assignments only")


class PageNotReady(ScrapeError):
    """The solve page showed no questions. `state` says why, `retry` says whether trying again later
    can help (a platform error can clear; an already-submitted assignment will not)."""
    TEXT = {
        "done":    ("هذا الواجب محلول أو مسلَّم من قبل", "already submitted", False),
        "closed":  ("هذا الواجب مقفل أو انتهى وقته في المنصة", "closed on the platform", False),
        "error":   ("المنصة رجعت صفحة خطأ أو صيانة", "platform error page", True),
        "login":   ("المنصة طلبت تسجيل دخول بدل ما تفتح الواجب", "login page instead of the assignment", True),
        "unknown": ("صفحة الواجب فتحت بدون أسئلة", "page opened without questions", True),
    }

    def __init__(self, state: str):
        ar, en, retry = self.TEXT.get(state, self.TEXT["unknown"])
        super().__init__(ar, en)
        self.state, self.retry = state, retry


class LayoutChanged(ScrapeError):
    """Fewer questions were read than the page shows: never solve (and later submit) half of it."""
    def __init__(self, read: int, seen: int):
        super().__init__(f"قريت {read} سؤال من أصل {seen}، شكل الصفحة تغيّر فما حليت عشان ما يطلع الحل ناقص",
                         f"read {read} of {seen} questions: page layout changed")
        self.read, self.seen = read, seen


_DONE = ("تم تسليم", "تم التسليم", "تم حل الواجب", "سبق لك", "لقد قمت بحل", "تم إرسال الإجابات")
_CLOSED = ("انتهى وقت", "انتهت مدة", "انتهى موعد", "انتهت فترة", "الواجب مغلق", "غير متاح حالياً", "غير متاح حاليا")
_ERROR = ("حدث خطأ", "خطأ في الخادم", "صيانة", "Service Unavailable", "Bad Gateway", "Server Error",
          "Gateway Time-out", "Too Many Requests")


def _visible_text(soup) -> str:
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return " ".join(soup.get_text(" ").split())


def page_state(html: str, url: str = "") -> str:
    """Why a solve page has no questions: login / done / closed / error / unknown."""
    if any(h in (url or "") for h in MS_HOSTS) or "loginfmt" in html:
        return "login"
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = _visible_text(soup)
    if any(w in text for w in _DONE):
        return "done"
    if any(w in text for w in _CLOSED):
        return "closed"
    if any(w in title or w in text for w in _ERROR):
        return "error"
    return "unknown"


def count_markers(html: str) -> int:
    """How many questions the page itself shows, counted WITHOUT the question parser: each question
    has a 'درجة السؤال' badge and a hidden question id. If the parser finds fewer, the layout changed."""
    soup = BeautifulSoup(html, "html.parser")
    badges = sum(1 for el in soup.select("[class*=badge]")
                 if re.search(r"درجة\s+السؤال", el.get_text(" ")))
    return max(badges, len(soup.select("input.qid")))


def _txt(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _is_exam(html: str, url: str = "") -> bool:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    return ("الاختبار" in title or "SolveLectureExam" in url
            or ("SolveStudentExam" in html and "Assignment" not in url))


def parse_questions(html: str) -> list[Question]:
    """Pure HTML -> questions (no browser). Screenshots are added by read_questions()."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Question] = []
    for n, card in enumerate(soup.select("div.dqc"), 1):
        qid_el, type_el = card.select_one("input.qid"), card.select_one("input.qidtype")
        if not qid_el:
            continue
        qid, qtype = qid_el.get("value", f"q{n}"), (type_el.get("value") if type_el else "")
        kind = TYPES.get(qtype, "unknown")
        head = _txt(card.select_one(".card-header h6"))
        body_el = card.select_one(".qQuestion") or card.select_one(".eldetail.clearfix")
        text = (head + "\n" + _txt(body_el)).strip()
        q = Question(id=f"q{n}", text=text, kind=kind, meta={"qid": qid, "type": qtype})
        if kind == "choice":
            for opt in card.select(".dqa .cs-input"):
                radio = opt.select_one("input[type=radio]")
                if radio:
                    q.options.append(_txt(opt))
                    q.meta.setdefault("values", []).append(radio.get("value"))
                    q.meta["name"] = radio.get("name")
        elif kind == "match":
            for li in card.select("ul.divMatchingQuestion li"):
                sel, left = li.select_one("select"), li.select_one("input.qaid")
                if sel and left:
                    q.prompts.append(_txt(li.select_one("label")))
                    q.meta.setdefault("left", []).append(left.get("value"))
            for li in card.select("ul.answer li"):
                right = li.select_one("input.qaid")
                if right:
                    q.options.append(_txt(li.select_one("label")))
                    q.meta.setdefault("right", []).append(right.get("value"))
        elif kind == "order":
            for li in card.select("ul.ui-sortable li[data-id], ul.divSoratble li[data-id]"):
                q.options.append(_txt(li.select_one(".col-md-8") or li))
                q.meta.setdefault("ids", []).append(li.get("data-id"))
        if kind != "unknown" and not q.options:
            q.kind = "unknown"
        out.append(q)
    return out


QUESTION_CARD = "div.dqc input.qid"
WAIT_MS = 20000          # longest wait for the first question card
SETTLE_MS = 1200         # the card count must stay the same this long before we read


def _wait_for_questions(page, timeout_ms: int | None = None) -> int:
    """Wait until question cards appear AND their count stops changing (the platform can draw
    them one by one after the page itself has loaded). Returns the final count, 0 if none."""
    import time
    timeout_ms = WAIT_MS if timeout_ms is None else timeout_ms
    end = time.monotonic() + timeout_ms / 1000
    last, since = -1, time.monotonic()
    while True:
        try:
            n = page.locator(QUESTION_CARD).count()
        except Exception:
            n = 0                              # page is in the middle of navigating
        now = time.monotonic()
        if n != last:
            last, since = n, now
        elif n > 0 and (now - since) * 1000 >= SETTLE_MS:
            return n
        if now >= end:
            return max(last, 0)
        page.wait_for_timeout(300)


def read_questions(page, allow_exam: bool = False) -> list[Question]:
    # exam guard first, before any waiting or reloading of the page
    if _is_exam(page.content(), page.url) and not allow_exam:
        raise NotAllowed()
    if _wait_for_questions(page) == 0:
        try:                                   # one fresh load: a slow or broken first load often clears
            page.reload(wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        _wait_for_questions(page)
    html = page.content()
    if _is_exam(html, page.url) and not allow_exam:
        raise NotAllowed()
    qs = parse_questions(html)
    seen = count_markers(html)
    if seen > len(qs):                         # questions on the page that the parser missed
        _snapshot(page, "layout_changed")
        raise LayoutChanged(len(qs), seen)
    if not qs:
        _snapshot(page, "no_questions")        # keep what the bot saw, to know why it was empty
        raise PageNotReady(page_state(html, page.url))
    cards = page.locator("div.dqc")
    for i, q in enumerate(qs):
        try:
            png = cards.nth(i).screenshot(type="jpeg", quality=70, timeout=8000)
            q.images.append("data:image/jpeg;base64," + base64.b64encode(png).decode())
        except Exception:
            pass                                   # text alone still works for plain questions
    return qs


# ---------------------------------------------------------------- filling
_FILL_JS = r"""
([kind, meta, ans]) => {
  const card = [...document.querySelectorAll('div.dqc')].find(c => (c.querySelector('input.qid')||{}).value === meta.qid);
  if (!card) return 'no-card';
  const jq = window.jQuery;
  if (kind === 'choice') {
    const v = meta.values[ans.choice - 1];
    const r = card.querySelector(`input[type=radio][value="${v}"]`);
    if (!r) return 'no-radio';
    r.click(); r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true}));
    return r.checked ? 'ok' : 'not-checked';
  }
  if (kind === 'match') {
    for (let i = 0; i < meta.left.length; i++) {
      const left = meta.left[i], right = meta.right[ans.seq[i] - 1];
      const sel = card.querySelector(`select.divAnswersModel_${left}`) ||
                  [...card.querySelectorAll('select')].find(s => s.dataset.secqid === left);
      if (!sel) return 'no-select';
      if (jq) jq(sel).val(right).trigger('change'); else { sel.value = right; sel.dispatchEvent(new Event('change', {bubbles: true})); }
      const hid = card.querySelector(`input.studentAnswer[id$="_${left}"]`);
      if (hid) hid.value = right;
      if (sel.value !== right) return 'not-selected';
    }
    return 'ok';
  }
  if (kind === 'order') {
    const ul = card.querySelector('ul.ui-sortable, ul.divSoratble');
    if (!ul) return 'no-list';
    const want = ans.seq.map(x => meta.ids[x - 1]);
    for (const id of want) {
      const li = ul.querySelector(`li[data-id="${id}"]`);
      if (!li) return 'no-item';
      ul.appendChild(li);
    }
    if (jq && jq.fn.sortable) { try { jq(ul).sortable('refreshPositions'); jq(ul).trigger('sortupdate'); } catch (e) {} }
    try { if (typeof refreshUpDownIcons === 'function') refreshUpDownIcons(jq ? jq(ul) : ul); } catch (e) {}
    const got = [...ul.querySelectorAll('li[data-id]')].map(li => li.dataset.id);
    return JSON.stringify(got) === JSON.stringify(want) ? 'ok' : 'wrong-order';
  }
  return 'skip';
}
"""

# Read back what the page really holds, after everything was filled (a later question can
# disturb an earlier one, e.g. a sortable list redrawing). Nothing is submitted unless it matches.
_READ_JS = r"""
([kind, meta]) => {
  const card = [...document.querySelectorAll('div.dqc')].find(c => (c.querySelector('input.qid')||{}).value === meta.qid);
  if (!card) return null;
  if (kind === 'choice') { const r = card.querySelector('input[type=radio]:checked'); return r ? r.value : null; }
  if (kind === 'match') return meta.left.map(left => {
      const sel = card.querySelector(`select.divAnswersModel_${left}`) ||
                  [...card.querySelectorAll('select')].find(s => s.dataset.secqid === left);
      return sel ? sel.value : null; });
  if (kind === 'order') { const ul = card.querySelector('ul.ui-sortable, ul.divSoratble');
      return ul ? [...ul.querySelectorAll('li[data-id]')].map(li => li.dataset.id) : null; }
  return null;
}
"""


def _wanted(q: Question, a: Answer):
    if q.kind == "choice":
        return q.meta["values"][a.choice - 1]
    if q.kind == "match":
        return [q.meta["right"][x - 1] for x in a.seq]
    if q.kind == "order":
        return [q.meta["ids"][x - 1] for x in a.seq]
    return None


def mismatches(page, questions: list[Question], answers: list[Answer]) -> list[str]:
    by_id = {a.qid: a for a in answers}
    bad = []
    for q in questions:
        a = by_id.get(q.id)
        if q.kind == "unknown" or not a:
            continue
        if page.evaluate(_READ_JS, [q.kind, q.meta]) != _wanted(q, a):
            bad.append(q.id)
    return bad


SUBMIT_TEXTS = ("حفظ وإنهاء", "إنهاء", "تسليم", "حفظ")


def fill(page, questions: list[Question], answers: list[Answer]) -> list[str]:
    """Put every available answer on the page. Returns ids of questions left unanswered."""
    by_id = {a.qid: a for a in answers}
    left_out = []
    for q in questions:
        a = by_id.get(q.id)
        has = a and ((q.kind == "choice" and a.choice) or (q.kind in ("match", "order") and a.seq))
        if q.kind == "unknown" or not has:
            left_out.append(q.id)
            continue
        res = page.evaluate(_FILL_JS, [q.kind, q.meta, {"choice": a.choice, "seq": a.seq}])
        if res != "ok":
            raise ScrapeError(f"ما قدرت أعبي السؤال ({res})", f"could not fill {q.id}: {res}")
    return left_out


def fill_and_submit(page, questions: list[Question], answers: list[Answer], allow_exam: bool = False) -> None:
    """Fill, press the page's own submit button, confirm its dialog, and only return when the
    platform shows it was saved. Raises otherwise (the caller then gives the student the link)."""
    if _is_exam(page.content(), page.url) and not allow_exam:
        raise NotAllowed()
    left_out = fill(page, questions, answers)
    if left_out:
        raise ScrapeError(f"فيه {len(left_out)} سؤال ما عندي له إجابة، حلّه بنفسك وسلّم",
                          f"{len(left_out)} questions without an answer")
    bad = mismatches(page, questions, answers)
    if bad:                                    # one more go for just those, then refuse
        fill(page, [q for q in questions if q.id in bad], answers)
        bad = mismatches(page, questions, answers)
    if bad:
        _snapshot(page, "not_matching")
        raise ScrapeError(f"{len(bad)} إجابة ما ثبتت في الصفحة، ما سلّمت عشان ما ينحسب غلط",
                          f"{len(bad)} answers did not stay on the page, not submitted")
    btn = page.locator("button[id^='Butto'], button[onclick*='Solve']").first
    if btn.count() == 0:
        for t in SUBMIT_TEXTS:
            cand = page.locator(f"button:has-text('{t}')")
            if cand.count():
                btn = cand.first
                break
    if btn.count() == 0:
        raise ScrapeError("ما لقيت زر التسليم", "submit button not found")
    before = page.url
    btn.click()
    # the platform asks "are you sure?" in a SweetAlert dialog
    try:
        page.locator(".swal2-confirm").first.click(timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(4000)
    _snapshot(page, "after_submit")
    ok_dialog = page.locator(".swal2-success, .swal2-icon-success").count() > 0
    if not (ok_dialog or page.url != before):
        raise Unconfirmed()


def _snapshot(page, name: str):
    if not SCREENSHOT_DIR:
        return
    try:
        import time
        from pathlib import Path
        d = Path(SCREENSHOT_DIR); d.mkdir(parents=True, exist_ok=True)
        stem = d / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
        page.screenshot(path=str(stem) + ".png", full_page=True)
        (Path(str(stem) + ".html")).write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
