"""One check cycle for one account: plan -> send -> record."""
from __future__ import annotations

from datetime import datetime

from .db import DB
from .parser import Item, RIYADH
from .reminders import plan, in_quiet_hours


def run_cycle(db: DB, account: str, items: list[Item], notifier, now: datetime | None = None) -> int:
    """Returns number of notifications sent.

    State rule: an item's stored state only advances once its notification (if any) was
    delivered, so a quiet-hours deferral or a failed send never loses an alert."""
    now = now or datetime.now(RIYADH)
    db.ensure_account(account)
    chat = db.chat_id(account)
    if chat is None:
        return 0                       # not linked to Telegram yet; nothing stored, nothing lost
    baselined = db.is_baselined(account)
    settings = db.get_settings(account)

    if baselined and in_quiet_hours(now, settings):
        return 0                       # freeze state; everything pending fires after quiet hours

    done = db.done_keys(account)       # submitted (by the bot or by hand): no alerts, not in summaries
    notices = plan([i for i in items if (i.kind, i.id) not in done], db.known(account), db.sent_events(account), settings, now, baselined,
                   solve_ok=db.solver_enabled(account))

    sent_n = 0
    failed: set[tuple[str, str]] = set()
    for n in notices:
        ok = notifier.send(chat, n.text, n.markup)
        if ok:
            sent_n += 1
            if n.event == "baseline":
                db.mark_baselined(account)
            elif n.kind != "summary":
                db.record_sent(account, n.kind, n.item_id, n.event, now)
            for kind, iid, ev in n.covers:
                db.record_sent(account, kind, iid, ev, now)
        elif n.event == "baseline":
            return sent_n              # retry whole baseline next cycle
        else:
            # keep the item 'unseen' so the next cycle retries this alert
            failed.add((n.kind, n.item_id))
            failed.update((k, i) for k, i, _ in n.covers)

    db.upsert_items(account, [i for i in items if (i.kind, i.id) not in failed], now)
    return sent_n