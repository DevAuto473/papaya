"""Reads questions from a Madrasati solve page and fills/submits answers (ASSIGNMENTS ONLY).

Layout learned from a saved solve page. Each question is `div.dqc` with hidden `.qid` and
`.qidtype`:  0 = multiple choice (radio), 2 = true/false (radio), 3 = matching (one <select> per
item of list (أ)), 6 = ordering (sortable <li data-id>). Anything else is reported as
"unknown" and left for the student. Exam pages are refused in code, not only by the UI."""
from __future__ import annotations

import base64

from bs4 import BeautifulSoup

from .scraper import ScrapeError
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


def read_questions(page, allow_exam: bool = False) -> list[Question]:
    html = page.content()
    if _is_exam(html, page.url) and not allow_exam:
        raise NotAllowed()
    qs = parse_questions(html)
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
