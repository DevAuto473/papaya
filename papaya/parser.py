"""Parse Madrasati 'واجباتي' / 'اختباراتي' pages (pure HTML -> Item list)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from typing import Optional

from bs4 import BeautifulSoup

RIYADH = timezone(timedelta(hours=3))
SITE = "https://schools.madrasati.sa/"   # links on the live page are relative

AR_MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "إبريل": 4, "ابريل": 4,
    "مايو": 5, "يونيو": 6, "يوليو": 7, "أغسطس": 8, "اغسطس": 8,
    "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}


@dataclass
class Item:
    kind: str                       # "assignment" | "exam"
    id: str
    title: str
    subject: str
    deadline: datetime              # tz-aware (Asia/Riyadh)
    status: str                     # Arabic status text as shown
    solve_url: Optional[str] = None
    opens_at: Optional[datetime] = None   # exams only, when known

    @property
    def is_open(self) -> bool:
        """True when the student can act on it now (not 'منتظر')."""
        return self.status.strip() != "منتظر"

    @property
    def external_submission(self) -> bool:
        return "قنوات أخرى" in self.status

    @property
    def done(self) -> bool:
        s = self.status
        return any(w in s for w in ("تم التسليم", "تم الحل", "مكتمل", "تم تسليم"))


def _dt(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%m/%d/%Y %H:%M:%S").replace(tzinfo=RIYADH)


def _subject(card) -> str:
    """Breadcrumb looks like: 'الثانوية ... - الفيزياء 3 - الوحدة - الدرس'. Take the course."""
    p = card.select_one("p")
    if not p:
        return ""
    parts = [x.strip() for x in re.split(r"\s+-\s+|\s-\s", p.get_text(" ", strip=True)) if x.strip()]
    # drop trailing "تصنيف الاختبار : ..." part
    parts = [x for x in parts if not x.startswith("تصنيف")]
    # structure: school, track, year, 'مقررات العام الدراسي', COURSE, unit, lesson
    for i, x in enumerate(parts):
        if x.startswith("مقررات") and i + 1 < len(parts):
            return parts[i + 1]
    return parts[-3] if len(parts) >= 3 else (parts[-1] if parts else "")


def _modal_start(modal) -> Optional[datetime]:
    """Modal row: 'تاريخ بداية الاختبار | 25/جمادى.../1448 - 05/نوفمبر/2026 10:54 ص'."""
    if modal is None:
        return None
    text = modal.get_text(" ", strip=True)
    m = re.search(r"تاريخ بداية الاختبار\s*(.+?)\s*تاريخ نهاية", text)
    if not m:
        return None
    seg = m.group(1)
    g = re.search(r"(\d{1,2})\W+([^\W\d_]+)\W+(\d{4})\s+(\d{1,2}):(\d{2})\s*([صم])", seg[seg.rfind("-"):])
    if not g:
        return None
    d, mon, y, hh, mm, ap = g.groups()
    month = AR_MONTHS.get(mon)
    if not month:
        return None
    hh = int(hh) % 12 + (12 if ap == "م" else 0)
    return datetime(int(y), month, int(d), hh, int(mm), tzinfo=RIYADH)


def _fallback_id(kind: str, title: str, deadline: datetime) -> str:
    return "h" + hashlib.sha1(f"{kind}|{title}|{deadline.isoformat()}".encode()).hexdigest()[:16]


def parse_assignments(html: str) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.dga-defualt-card"):
        t = card.select_one("time.assignment-timeago")
        h = card.select_one("h4")
        if not t or not h:
            continue
        title = h.get_text(" ", strip=True)
        deadline = _dt(t["datetime"])
        iid = None
        for a in card.select("a[onclick]"):
            m = re.search(r"view\('([0-9A-Fa-f]+)'", a["onclick"])
            if m:
                iid = m.group(1)
                break
        solve = card.select_one("a.analytics-SolveAssignment[href]")
        if not iid and solve:
            m = re.search(r"SolveLectureAssignment/([0-9A-Fa-f]+)", solve["href"])
            iid = m and m.group(1)
        badge = card.select_one(".status-badge")
        items.append(Item(
            kind="assignment",
            id=iid or _fallback_id("assignment", title, deadline),
            title=title,
            subject=_subject(card),
            deadline=deadline,
            status=badge.get_text(" ", strip=True) if badge else "",
            solve_url=urljoin(SITE, solve["href"]) if solve else None,
        ))
    return items


def parse_exams(html: str) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.dga-defualt-card"):
        t = card.select_one("time.exam-timeago")
        h = card.select_one("h4")
        if not t or not h:
            continue
        title = h.get_text(" ", strip=True)
        deadline = _dt(t["datetime"])
        iid, modal = None, None
        for a in card.select("a[onclick]"):
            m = re.search(r"showpopup\('InfoDiv(\d+)'", a["onclick"])
            if m:
                iid = m.group(1)
                modal = soup.find(id=f"InfoDiv{iid}")
                break
        solve = card.select_one("a[href*=SolveLectureExam]")
        badge = card.select_one(".status-badge")
        items.append(Item(
            kind="exam",
            id=iid or _fallback_id("exam", title, deadline),
            title=title,
            subject=_subject(card),
            deadline=deadline,
            status=badge.get_text(" ", strip=True) if badge else "",
            solve_url=urljoin(SITE, solve["href"]) if solve else None,
            opens_at=_modal_start(modal),
        ))
    return items


def page_title(html: str) -> str:
    """Text of the page heading ('واجباتي' / 'اختباراتي'); '' if the layout is unexpected."""
    el = BeautifulSoup(html, "html.parser").select_one(".mainTitlePage")
    return el.get_text(strip=True) if el else ""
