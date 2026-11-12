# Graph Report - papaya  (2026-11-12)

## Corpus Check
- Corpus is ~18,489 words - fits in a single context window. You may not need a graph.

## Summary
- 373 nodes · 980 edges · 11 communities
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 102 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Browser Scraper & Login
- Telegram Bot UI
- Reminder Planning
- Check Cycle & DB Core
- Solve Service
- Assignment Page Automation
- CLI Entry & Config
- AI Client & Notifier
- Health, Backup & Meta
- Platform HTML Parser
- Account Check Flow

## God Nodes (most connected - your core abstractions)
1. `DB` - 60 edges
2. `Bot` - 35 edges
3. `main()` - 30 edges
4. `Item` - 29 edges
5. `SolveService` - 24 edges
6. `cycle_loop()` - 22 edges
7. `run_cycle()` - 21 edges
8. `check_account()` - 20 edges
9. `plan()` - 16 edges
10. `auto_first_login()` - 15 edges

## Surprising Connections (you probably didn't know these)
- `_log_sent()` --indirect_call--> `chat()`  [INFERRED]
  main.py → ai.py
- `settings_view()` --uses--> `DB`  [INFERRED]
  bot.py → db.py
- `Bot` --uses--> `DB`  [INFERRED]
  bot.py → db.py
- `Bot` --uses--> `Item`  [INFERRED]
  bot.py → parser.py
- `run_cycle()` --uses--> `Item`  [INFERRED]
  cycle.py → parser.py

## Import Cycles
- None detected.

## Communities (11 total, 0 thin omitted)

### Community 0 - "Browser Scraper & Login"
Cohesion: 0.07
Nodes (36): page_title(), _active_page(), _all_pages(), auto_first_login(), auto_login(), bind_email(), bound_email(), check_identity() (+28 more)

### Community 1 - "Telegram Bot UI"
Cohesion: 0.09
Nodes (9): Bot, run(), guide(), _hour(), _is_list_msg(), kb(), menu(), settings_view() (+1 more)

### Community 2 - "Reminder Planning"
Cohesion: 0.12
Nodes (23): Settings, Item, _ai_button(), button_for(), _clock(), _count(), _day_label(), digest() (+15 more)

### Community 3 - "Check Cycle & DB Core"
Cohesion: 0.09
Nodes (6): _prune(), run_cycle(), DB, autoclean(), _still_open(), verify()

### Community 4 - "Solve Service"
Cohesion: 0.12
Nodes (9): budget_usd(), _fail_text(), _opt(), preview_markup(), render_preview(), _short(), SolveService, run() (+1 more)

### Community 5 - "Assignment Page Automation"
Cohesion: 0.11
Nodes (21): fill(), fill_and_submit(), _is_exam(), mismatches(), NotAllowed, parse_questions(), read_questions(), _snapshot() (+13 more)

### Community 6 - "CLI Entry & Config"
Cohesion: 0.15
Nodes (14): Api, accounts(), admin_id(), load_env(), _admin_notifier(), cycle_loop(), _interval(), _log_sent() (+6 more)

### Community 7 - "AI Client & Notifier"
Cohesion: 0.13
Nodes (6): AIError, chat(), extract_json(), models(), Reply, ConsoleNotifier

### Community 8 - "Health, Backup & Meta"
Cohesion: 0.18
Nodes (4): backup(), _fmt_gap(), heartbeat(), network_gate()

### Community 9 - "Platform HTML Parser"
Cohesion: 0.21
Nodes (8): _dt(), _fallback_id(), _modal_start(), parse_assignments(), parse_exams(), _subject(), finished_ids(), mask()

### Community 10 - "Account Check Flow"
Cohesion: 0.29
Nodes (3): check_account(), _layout_check(), _unique()

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `DB` connect `Check Cycle & DB Core` to `Telegram Bot UI`, `Reminder Planning`, `CLI Entry & Config`, `Health, Backup & Meta`, `Account Check Flow`?**
  _High betweenness centrality (0.183) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `DB` (e.g. with `Bot` and `settings_view()`) actually correct?**
  _`DB` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Should `Browser Scraper & Login` be split into smaller, more focused modules?**
  _Cohesion score 0.06547619047619048 - nodes in this community are weakly interconnected._
- **Why does `Bot` connect `Telegram Bot UI` to `Reminder Planning`, `Check Cycle & DB Core`, `CLI Entry & Config`?**
  _High betweenness centrality (0.146) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `Bot` (e.g. with `DB` and `Item`) actually correct?**
  _`Bot` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Should `Telegram Bot UI` be split into smaller, more focused modules?**
  _Cohesion score 0.08549019607843138 - nodes in this community are weakly interconnected._
- **Why does `SolveService` connect `Solve Service` to `CLI Entry & Config`, `AI Client & Notifier`?**
  _High betweenness centrality (0.104) - this node is a cross-community bridge._