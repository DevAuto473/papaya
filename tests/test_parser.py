import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from papaya.parser import parse_assignments, parse_exams

F = pathlib.Path(__file__).resolve().parent / "fixtures"
A = (F / "assignments.htm").read_text(encoding="utf-8")
E = (F / "exams.htm").read_text(encoding="utf-8")


def test_assignments():
    items = parse_assignments(A)
    assert len(items) == 15
    assert len({i.id for i in items}) == 15
    assert all(i.subject for i in items)
    assert any(i.external_submission for i in items)
    assert any(i.solve_url for i in items)
    assert all(i.deadline.utcoffset().total_seconds() == 3 * 3600 for i in items)


def test_exams():
    items = parse_exams(E)
    assert len(items) == 11
    assert len({i.id for i in items}) == 11
    waiting = [i for i in items if not i.is_open]
    assert waiting and all(i.solve_url is None for i in waiting)
    assert all(i.opens_at and i.opens_at < i.deadline for i in items), [i.title for i in items if not i.opens_at]


def test_exam_solve_link_published_exam():
    """Live layout (Nov 2026): the 'حل الاختبار' button links to SolvePublishedExam."""
    html = """
    <div class="dga-defualt-card">
      <h4>اختبار الأسبوع الحادي عشر</h4>
      <p>الثانوية العامة - المسار العام - السنة الثالثة - مقررات العام الدراسي - التنمية المستدامة - الأمن المائي</p>
      <time class="exam-timeago" datetime="11/19/2026 17:26:00"></time>
      <span class="status-badge">بانتظار حل الاختبار</span>
      <a class="btn btn-primary" href="/Student/Exams/SolvePublishedExam/2DD75E50?SchoolId=1&amp;UId=2">حل الاختبار</a>
      <a class="btn" onclick="showpopup('InfoDiv325651');">تفاصيل الاختبار</a>
    </div>"""
    [it] = parse_exams(html)
    assert it.id == "325651" and it.is_open
    assert it.solve_url == "https://schools.madrasati.sa/Student/Exams/SolvePublishedExam/2DD75E50?SchoolId=1&UId=2"


if __name__ == "__main__":
    for it in parse_assignments(A) + parse_exams(E):
        print(it.kind, it.id[:10], it.subject, "|", it.title[:25], "|", it.deadline.strftime("%m-%d %H:%M"),
              "|", it.opens_at and it.opens_at.strftime("%m-%d %H:%M"), "|", it.status, "|", bool(it.solve_url))
