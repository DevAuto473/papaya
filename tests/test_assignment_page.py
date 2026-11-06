"""Reader/filler against a saved solve page served locally (no contact with the platform)."""
import functools, http.server, pathlib, shutil, sys, tempfile, threading
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pytest.importorskip("playwright")
from papaya import assignment_page as ap
from papaya.scraper import ScrapeError
from papaya.solver import Answer

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "solve_sample.htm"
pytestmark = pytest.mark.skipif(not FIX.exists(), reason="no saved solve page")


def test_parse_all_question_types():
    qs = ap.parse_questions(FIX.read_text(encoding="utf-8"))
    kinds = [q.kind for q in qs]
    assert kinds.count("choice") == 5 and "match" in kinds and "order" in kinds
    m = next(q for q in qs if q.kind == "match")
    assert len(m.prompts) == len(m.meta["left"]) == 3 and len(m.options) == len(m.meta["right"]) == 3
    o = next(q for q in qs if q.kind == "order")
    assert len(o.options) == len(o.meta["ids"]) == 5


def test_exam_pages_are_refused():
    assert ap._is_exam(FIX.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright
    d = pathlib.Path(tempfile.mkdtemp()); shutil.copy(FIX, d / "p.htm")
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(base) else r.abort())
        pg.goto(base + "/p.htm", wait_until="domcontentloaded")
        yield pg
        b.close()
    srv.shutdown()


def test_reader_refuses_exam_and_reads_with_override(page):
    with pytest.raises(ap.NotAllowed):
        ap.read_questions(page)
    qs = ap.read_questions(page, allow_exam=True)
    assert len(qs) == 7 and all(q.images for q in qs)          # every question photographed


def test_fill_every_type_then_state_matches(page):
    qs = ap.read_questions(page, allow_exam=True)
    ans = []
    for q in qs:      # arbitrary (not correct) answers: we only test that the page takes them
        if q.kind == "choice":
            ans.append(Answer(q.id, len(q.options), None, "", True))
        elif q.kind == "match":
            ans.append(Answer(q.id, None, None, "", True, seq=list(range(len(q.prompts), 0, -1))))
        elif q.kind == "order":
            ans.append(Answer(q.id, None, None, "", True, seq=list(range(len(q.options), 0, -1))))
    assert ap.fill(page, qs, ans) == []
    for q, a in zip(qs, ans):
        if q.kind == "choice":
            assert page.locator(f'input[type=radio][value="{q.meta["values"][-1]}"]').is_checked()
        if q.kind == "order":
            got = page.eval_on_selector_all(f'#foo{q.meta["qid"]} li[data-id]', "els => els.map(e => e.dataset.id)")
            assert got == [q.meta["ids"][x - 1] for x in a.seq]
        if q.kind == "match":
            for left, x in zip(q.meta["left"], a.seq):
                assert page.eval_on_selector(f"select.divAnswersModel_{left}", "s => s.value") == q.meta["right"][x - 1]


def test_submit_without_platform_confirmation_raises(page):
    qs = ap.read_questions(page, allow_exam=True)
    ans = [Answer(q.id, 1 if q.kind == "choice" else None, None, "", True,
                  seq=list(range(1, (len(q.prompts) if q.kind == "match" else len(q.options)) + 1))
                  if q.kind in ("match", "order") else None) for q in qs]
    with pytest.raises(ScrapeError):
        ap.fill_and_submit(page, qs, ans)                       # exam guard
    with pytest.raises(ScrapeError) as e:
        ap.fill_and_submit(page, qs, ans, allow_exam=True)      # no real server -> never claims success
    assert "ما تأكدت" in e.value.ar or "زر" in e.value.ar


def test_missing_answer_blocks_submit(page):
    qs = ap.read_questions(page, allow_exam=True)
    with pytest.raises(ScrapeError) as e:
        ap.fill_and_submit(page, qs, [], allow_exam=True)
    assert "ما عندي له إجابة" in e.value.ar
