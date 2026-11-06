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


if __name__ == "__main__":
    for it in parse_assignments(A) + parse_exams(E):
        print(it.kind, it.id[:10], it.subject, "|", it.title[:25], "|", it.deadline.strftime("%m-%d %H:%M"),
              "|", it.opens_at and it.opens_at.strftime("%m-%d %H:%M"), "|", it.status, "|", bool(it.solve_url))
