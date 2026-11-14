"""read_questions must wait for questions the platform draws late, and one by one."""
import pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pytest.importorskip("playwright")
from papaya import assignment_page as ap

CARD = ('<div class="dqc"><input type="hidden" class="qid" value="Q{n}">'
        '<input type="hidden" class="qidtype" value="0">'
        '<div class="card-header"><h6>اختر الإجابة الصحيحة</h6></div>'
        '<div class="eldetail clearfix">سؤال رقم {n}</div>'
        '<div class="dqa"><div class="cs-input"><input type="radio" name="{n}" value="a{n}"> أ</div>'
        '<div class="cs-input"><input type="radio" name="{n}" value="b{n}"> ب</div></div></div>')


def page_html(script: str) -> str:
    return ("<html><head><title>مدرستي | الواجبات</title></head><body><div id='box'></div>"
            "<script>const CARD = " + repr(CARD) + ";\n"
            "function add(n){ document.getElementById('box').insertAdjacentHTML('beforeend', "
            "CARD.replaceAll('{n}', n)); }\n" + script + "</script></body></html>")


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def test_questions_drawn_late_are_read(browser):
    pg = browser.new_page()
    pg.set_content(page_html("setTimeout(() => { add(1); add(2); add(3); }, 4000);"))
    qs = ap.read_questions(pg)
    assert len(qs) == 3 and all(q.kind == "choice" and len(q.options) == 2 for q in qs)
    pg.close()


def test_questions_drawn_one_by_one_are_all_read(browser):
    pg = browser.new_page()
    pg.set_content(page_html("[1,2,3,4,5].forEach((n, i) => setTimeout(() => add(n), 500 + i * 700));"))
    qs = ap.read_questions(pg)
    assert len(qs) == 5                       # not just the first card that appeared
    pg.close()


def test_really_empty_page_says_why(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "WAIT_MS", 1500)
    monkeypatch.setattr(ap, "SCREENSHOT_DIR", str(tmp_path))
    pg = browser.new_page()      # served at a URL so the bot's reload gets the same page back
    body = page_html("document.body.insertAdjacentHTML('beforeend', '<p>تم تسليم الواجب</p>');")
    pg.route("http://test.local/**", lambda r: r.fulfill(body=body, content_type="text/html; charset=utf-8"))
    pg.goto("http://test.local/solve")
    with pytest.raises(ap.PageNotReady) as e:
        ap.read_questions(pg)
    assert e.value.state == "done" and not e.value.retry
    assert list(tmp_path.glob("*_no_questions.html"))    # what the bot saw is kept
    pg.close()


def test_new_layout_is_caught_not_half_solved(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "WAIT_MS", 1500)
    monkeypatch.setattr(ap, "SCREENSHOT_DIR", str(tmp_path))
    pg = browser.new_page()      # 3 questions, but 2 of them in a wrapper the parser does not know
    pg.set_content(page_html("add(1); for (const n of [2,3]) document.getElementById('box').insertAdjacentHTML("
                             "'beforeend', CARD.replaceAll('{n}', n).replace('class=\"dqc\"', 'class=\"q-new\"'));"))
    with pytest.raises(ap.LayoutChanged) as e:
        ap.read_questions(pg)
    assert (e.value.read, e.value.seen) == (1, 3)
    pg.close()


@pytest.mark.parametrize("html,state", [
    ("<form><input name='loginfmt'></form>", "login"),
    ("<p>لقد قمت بحل هذا الواجب</p>", "done"),
    ("<p>انتهى وقت الواجب</p>", "closed"),
    ("<title>503 Service Unavailable</title>", "error"),
    ("<title>مدرستي | حدث خطأ ما</title><h4>ليس لديك صلاحية لإتمام العملية</h4>", "denied"),
    ("<p>صفحة فاضية</p>", "unknown"),
    ("<p>الصيانة الدورية لمواسير الماء</p>", "unknown"),            # a question about maintenance
    ("<h1>الموقع تحت الصيانة</h1>", "error"),
    ("<p>حدث خطأ</p>" + "<p>نص طويل</p>" * 200, "unknown"),         # a long page that just mentions it
    ("<script>var x='تم تسليم'</script><p>شي</p>", "unknown"),      # words inside scripts don't count
])
def test_page_state(html, state):
    assert ap.page_state(html) == state


def test_markers_count_real_badges():
    card = CARD.replace('<div class="card-header">',
                        '<div class="card-header"><div class="badge badge-timeago">درجة السؤال 1.00</div>')
    html = "".join(card.replace("{n}", str(n)) for n in (1, 2, 3, 4))
    assert ap.count_markers(html) == 4 and len(ap.parse_questions(html)) == 4
