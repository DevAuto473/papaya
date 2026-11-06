"""Pure reminder logic: decide which notifications are due. No I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from .db import Settings
from .parser import Item, RIYADH

KIND_AR = {"assignment": "واجب", "exam": "اختبار"}
DAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


@dataclass
class Notice:
    kind: str       # item kind, or "summary"
    item_id: str
    event: str      # new | remind | baseline | batch
    text: str       # Telegram HTML
    markup: dict | None = None                       # link buttons under the message
    covers: list = field(default_factory=list)       # extra (kind, id, event) marked sent with it


def in_quiet_hours(now: datetime, s: Settings) -> bool:
    h = now.astimezone(RIYADH).hour
    a, b = s.quiet_start, s.quiet_end
    if a == b:
        return False
    return (a <= h or h < b) if a > b else (a <= h < b)


def fmt_dt(d: datetime) -> str:
    d = d.astimezone(RIYADH)
    ap = "ص" if d.hour < 12 else "م"
    h12 = d.hour % 12 or 12
    return f"{DAYS_AR[d.weekday()]} {d.day}/{d.month} {h12}:{d.minute:02d} {ap}"


def _count(n: int, one: str, two: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n == 2:
        return two
    return f"{n} {few if 3 <= n <= 10 else many}"


def fmt_left(delta: timedelta) -> str:
    """'يومين و5 ساعات' / '8 ساعات' / '40 دقيقة' — what the platform shows, in plain Arabic."""
    mins = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(mins, 24 * 60)
    hours, m = divmod(rem, 60)
    H = lambda n: _count(n, "ساعة", "ساعتين", "ساعات", "ساعة")
    if days >= 3:
        return _count(days, "يوم", "يومين", "أيام", "يوم")
    if days >= 1:
        d = _count(days, "يوم", "يومين", "أيام", "يوم")
        return d + (f" و{H(hours)}" if hours else "")
    if hours >= 3:
        return H(hours)
    if hours >= 1:
        return H(hours) + (f" و{_count(m, 'دقيقة', 'دقيقتين', 'دقائق', 'دقيقة')}" if m >= 5 else "")
    return _count(max(m, 1), "دقيقة", "دقيقتين", "دقائق", "دقيقة")


def status_icon(it: Item) -> str:
    if it.external_submission:
        return "📨"          # submitted outside the platform
    if not it.is_open:
        return "🔒"          # 'منتظر' — not open yet
    return "✍️"              # 'بانتظار حل ...' — open, waiting for you


LEGEND = "مفتوح للحل ✍️ · يُسلَّم خارج المنصة 📨 · لم يُفتح بعد 🔒"


def _day_label(d: datetime, now: datetime) -> str:
    days = (d.astimezone(RIYADH).date() - now.astimezone(RIYADH).date()).days
    if days == 0:
        return "اليوم 🔴"
    if days == 1:
        return "بكرة 🟠"
    dd = d.astimezone(RIYADH)
    return f"{DAYS_AR[dd.weekday()]} {dd.day}/{dd.month} 🟢"


def _clock(d: datetime) -> str:
    d = d.astimezone(RIYADH)
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'ص' if d.hour < 12 else 'م'}"


def digest(items: list[Item], now: datetime, header: str, days_ahead: int | None = None) -> str:
    """Upcoming items grouped by day; each line = status icon, kind, subject, time, time left."""
    live = sorted((i for i in items if i.deadline > now and not i.done), key=lambda i: i.deadline)
    if not live:
        return header + "\n\nما عندك مواعيد قادمة 🎉"
    shown = live if days_ahead is None else [
        i for i in live if (i.deadline.astimezone(RIYADH).date() - now.astimezone(RIYADH).date()).days < days_ahead]
    lines = [header]
    label = None
    for i in shown:
        lab = _day_label(i.deadline, now)
        if lab != label:
            lines.append(f"\n<b>{lab}</b>")
            label = lab
        lines.append(f"{KIND_AR[i.kind]} {escape(i.subject)} · {_clock(i.deadline)} · باقي {fmt_left(i.deadline - now)} {status_icon(i)}")
    rest = len(live) - len(shown)
    if rest:
        lines.append(f"\n… و{rest} بعدها — /list للكل")
    lines.append(f"\n<i>{LEGEND}</i>")
    return "\n".join(lines)


BASE_URL = "https://schools.madrasati.sa"


def solve_link(it: Item) -> str | None:
    """Same address as the platform's own 'حل الواجب' / 'حل الاختبار' button."""
    if not it.solve_url or it.external_submission or not it.is_open:
        return None
    u = it.solve_url
    return u if u.startswith("http") else BASE_URL + "/" + u.lstrip("/")


def _ai_button(it: Item) -> dict:
    return {"text": "حلّه لي 🆘", "callback_data": f"solve:{it.id}"}


def done_button(it: Item, label: str = "سلّمته ✅") -> dict:
    return {"text": label, "callback_data": f"done:{'a' if it.kind == 'assignment' else 'e'}:{it.id}"}


def button_for(it: Item, solve_ok: bool = False) -> dict | None:
    """Link button; plus the AI button for assignments ONLY when the admin granted it."""
    u = solve_link(it)
    if not u:
        return None
    label = "حل الواجب ✍️" if it.kind == "assignment" else "فتح الاختبار 🧪"
    rows = [[{"text": label, "url": u}]]
    if it.kind == "exam":
        rows[0].append(done_button(it, "✅"))
    if solve_ok:
        rows.append([_ai_button(it)])
    return {"inline_keyboard": rows}


def list_buttons(items: list, now: datetime, limit: int = 8, solve_ok: bool = False,
                 loading: dict | None = None) -> dict | None:
    """loading: {item_id: label} for items being solved right now (their 🆘 becomes a spinner)."""
    rows, ticks, n = [], [], 0
    for it in sorted((i for i in items if i.deadline > now and not i.done), key=lambda i: i.deadline):
        u = solve_link(it)
        if u:
            icon = "✍️" if it.kind == "assignment" else "🧪"
            num = ""
            if it.kind == "exam":                 # small numbered ✅ buttons go below, so the names stay readable
                n += 1; num = f"{n}. "
                ticks.append(done_button(it, f"{n} ✅"))
            row = [{"text": f"{num}{KIND_AR[it.kind]} {it.subject} · باقي {fmt_left(it.deadline - now)} {icon}", "url": u}]
            if solve_ok:
                row.append({"text": loading[it.id], "callback_data": "noop:-"} if loading and it.id in loading
                           else {"text": "🆘", "callback_data": f"solve:{it.id}"})
            rows.append(row)
        if len(rows) >= limit:
            break
    rows += [ticks[i:i + 4] for i in range(0, len(ticks), 4)]
    return {"inline_keyboard": rows} if rows else None


def in_horizon(it: Item, now: datetime, days: int) -> bool:
    if days <= 0:
        return True
    if days == 1:                                 # "24 ساعة": what ends within the next 24 hours
        return it.deadline - now <= timedelta(hours=24)
    return (it.deadline.astimezone(RIYADH).date() - now.astimezone(RIYADH).date()).days < days


def wanted(it: Item, s: Settings) -> bool:
    return s.remind_external or not it.external_submission


HORIZON_AR = {1: "24 ساعة", 2: "اليوم وبكرة", 3: "3 أيام", 7: "أسبوع", 0: "الكل"}


def _state_text(it: Item) -> str:
    if it.external_submission:
        return "يُسلَّم عن طريق قنوات أخرى 📨"
    if not it.is_open:
        return "لم يُفتح للحل بعد 🔒"
    return "مفتوح — بانتظار حلك ✍️"


def _line(it: Item) -> str:
    k = KIND_AR[it.kind]
    head = it.title if it.title.startswith(k) else f"{k}: {it.title}"
    return f"<b>{escape(head)}</b>\n{escape(it.subject)} 📚\n{_state_text(it)}"


def _heads_up_text(it: Item, prev, now: datetime) -> str:
    if prev is None:
        tag = "جديد 🆕"
    elif (prev["status"] or "").strip() == "منتظر":
        tag = "انفتح الآن 🔓"
    else:
        tag = "يقترب موعده 📅"
    return f"{tag}\n{_line(it)}\nينتهي: {fmt_dt(it.deadline)} (باقي {fmt_left(it.deadline - now)}) ⏰"


def _remind_text(it: Item, now: datetime) -> str:
    return f"باقي {fmt_left(it.deadline - now)} ⏳\n{_line(it)}\nينتهي: {fmt_dt(it.deadline)} ⏰"


def plan(items: list[Item], known: dict, sent: set, s: Settings, now: datetime,
         baselined: bool, solve_ok: bool = False) -> list[Notice]:
    """Return notices due *now*. Nothing is ever repeated (see `sent`).

    Only items that are OPEN and due within the user's horizon (default: today + tomorrow)
    produce notifications. Locked ('منتظر') items wait silently until they open.
    Quiet hours are handled by the caller (state is frozen, so nothing is lost)."""
    live = [i for i in items if i.deadline > now and not i.done and wanted(i, s)]
    soon = sorted((i for i in live if i.is_open and in_horizon(i, now, s.horizon_days)),
                  key=lambda i: i.deadline)

    if not baselined:
        head = (f"<b>تم ربط الحساب</b> ✅\nالمفتوح وينتهي خلال {HORIZON_AR.get(s.horizon_days, f'{s.horizon_days} أيام')}: "
                f"{sum(i.kind == 'assignment' for i in soon)} واجب و{sum(i.kind == 'exam' for i in soon)} اختبار")
        return [Notice("summary", "-", "baseline", digest(soon, now, head), list_buttons(soon, now, solve_ok=solve_ok),
                       covers=[(i.kind, i.id, "new") for i in soon])]

    if in_quiet_hours(now, s):
        return []

    heads, reminds = [], []
    for it in soon:
        k = (it.kind, it.id)
        need_head = s.notify_new and (it.kind, it.id, "new") not in sent
        need_rem = (s.notify_remind and it.deadline - now <= timedelta(hours=s.remind_hours)
                    and (it.kind, it.id, "remind") not in sent)
        if need_rem:      # one message is enough when both are due
            reminds.append(Notice(it.kind, it.id, "remind", _remind_text(it, now), button_for(it, solve_ok),
                                  covers=[(it.kind, it.id, "new")] if need_head else []))
        elif need_head:
            heads.append((it, Notice(it.kind, it.id, "new", _heads_up_text(it, known.get(k), now), button_for(it, solve_ok))))

    singles = reminds + [n for _, n in heads]
    if len(singles) > 2:  # several at once (e.g. every morning): ONE grouped message
        by_key = {(i.kind, i.id): i for i in soon}
        its = [by_key[(n.kind, n.item_id)] for n in singles]
        covers = [(n.kind, n.item_id, n.event) for n in singles] + [c for n in singles for c in n.covers]
        return [Notice("summary", "-", "batch",
                       digest(its, now, f"<b>{len(its)} مواعيد قريبة</b> 🔔"), list_buttons(its, now, solve_ok=solve_ok),
                       covers=covers)]
    return singles