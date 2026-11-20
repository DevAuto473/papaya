# Auto-solve checklist (24h window)

Auto-solve picks up exams and assignments **24 hours** before the deadline
(`AUTO_SOLVE_BEFORE` in `papaya/service.py`). Run through this after every change
to the solver, and once a day while the 24h window is new.

## Before changing code
- [ ] `./upd.sh begin` (fresh snapshot — a stale one wipes recent edits on rollback)
- [ ] Check file mtimes; the owner edits files live

## Automated tests (`.venv/bin/python -m pytest -q tests`)
- [x] Nothing is solved when more than 24h remain (24h21m left -> `[]`)
- [x] Solved inside the window (23h21m left -> `ready`)
- [x] Exams **and** assignments are both handled, never anything beyond 24h
- [x] Auto-submit never happens when the models disagree
- [x] Auto-submit (preview mode) waits until 1h before the deadline
- [x] "ليس لديك صلاحية" page = `locked` (not open yet), not a platform error
- [x] Locked item: no in-place retries, no student message, no admin alert, retried after 1h
- [x] Any other auto failure: no student message, retried after 30 min (not every cycle)
- [x] Manual solve of a locked item still tells the student why
- [x] Normal (non-VIP) users never get solving
- [x] `./upd.sh done` -> "code checks passed" + "bot restarted and running"

## Live checks (after restart)
- [x] `PYTHONPATH=. .venv/bin/python diagnose/window24.py` lists the items inside 24h per account
- [x] `logs/bot.log` shows `auto-solver -> ...` lines after each account's check
- [x] No `auto-solver crashed` / `Traceback` in the log
- [x] Results are `ready` / `submitted` / `locked`; `page_error` and `empty` are rare
- [ ] Same item is **not** tried again in the next cycle after a failure
- [x] Students get no "⚠️" error messages from automatic attempts
- [x] New `diagnose/*_no_questions.png` files: open them — a new kind of page needs a new state
- [ ] Monthly AI spend still under the cap (wider window = more items solved)

## Known risks of the 24h window
- [ ] **Direct submit is ON for every account**: solving and submitting happen together, so work
      can be submitted up to 24h early. Turn direct submit off if submission should wait until 1h before.
- [ ] Items listed but not published by the teacher (`published=false`) show as `locked` until opened.
- [ ] Backoff memory is in RAM: a restart allows one immediate retry per item (fine, just expected).

## Last run — 2026-11-14 12:49–12:57 (first cycle on the new code)
- papaya03: assignment «واجب (المكان)» solved 1/1 + submitted; exam «اختبار قصير» solved 9/9 + submitted
  (confirmed on the platform); 1 `page_error` -> will retry quietly after 30 min
- papaya02 (x2), papaya04, papaya05: `locked` — the platform says "ليس لديك صلاحية" (not published yet)
- No crashes, no error messages sent to students
