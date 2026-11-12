#!/usr/bin/env bash
# Safe updates for papaya.
#   ./upd.sh begin   save a copy of the code (do this before pasting update commands)
#   ./upd.sh done    check the new code; if it is broken, put the copy back by itself; else restart the bot
#   ./upd.sh undo    put the last copy back and restart
cd "$HOME/papaya" || exit 1
SNAP=.snapshots
PY=.venv/bin/python
restart() {
  if pgrep -f papaya/run.sh >/dev/null; then
    n=$(wc -l < logs/bot.log 2>/dev/null || echo 0)
    pkill -f "papaya.main run"
    for i in $(seq 1 30); do
      sleep 2
      if tail -n +$((n + 1)) logs/bot.log 2>/dev/null | grep -q "papaya is running"; then echo "[ok] bot restarted and running"; return 0; fi
      if tail -n +$((n + 1)) logs/bot.log 2>/dev/null | grep -q "Traceback"; then echo "[fail] bot crashed on start:"; tail -n +$((n + 1)) logs/bot.log | tail -8; return 1; fi
    done
    echo "[warn] no 'papaya is running' yet - look at: tail -20 ~/papaya/logs/bot.log"; return 1
  else
    echo "[info] the bot is not running from run.sh - start it yourself"; return 0
  fi
}
check() {
  "$PY" - <<'PYEOF'
import compileall, importlib, sys
ok = bool(compileall.compile_dir("papaya", quiet=1))
for m in ("db", "parser", "reminders", "cycle", "notifier", "ai", "solver", "assignment_page",
          "scraper", "service", "bot", "main"):
    try:
        importlib.import_module("papaya." + m)
    except Exception as e:
        print(f"[fail] papaya/{m}.py: {type(e).__name__}: {e}"); ok = False
if ok:
    from papaya.db import DB
    from papaya.bot import Bot
    db = DB(":memory:")
    class Api:
        def call(self, *a, **k): return {"message_id": 1}
    try:
        Bot(Api(), db, 1, ["papaya01"]).list_view("papaya01", __import__("datetime").datetime.now(__import__("papaya.parser").parser.RIYADH))
    except Exception as e:
        print(f"[fail] bot self-test: {type(e).__name__}: {e}"); ok = False
print("[ok] code checks passed" if ok else "[fail] code is broken")
sys.exit(0 if ok else 1)
PYEOF
}
restore() {
  last=$(cat "$SNAP/last" 2>/dev/null)
  [ -n "$last" ] && [ -d "$SNAP/$last" ] || { echo "[fail] no saved copy"; exit 1; }
  rm -rf papaya && cp -r "$SNAP/$last" papaya && echo "[ok] code put back to the copy from $last"
}
case "$1" in
  begin)
    mkdir -p "$SNAP"; ts=$(date +%Y%m%d-%H%M%S)
    cp -r papaya "$SNAP/$ts" && echo "$ts" > "$SNAP/last"
    ls -1d "$SNAP"/2* 2>/dev/null | head -n -10 | xargs -r rm -rf
    echo "[ok] copy saved ($ts). paste the update commands, then run: ./upd.sh done" ;;
  done)
    if check; then restart || { echo "[fail] putting the old code back"; restore; restart; }
    else echo "[fail] the update is broken - putting the old code back"; restore; echo "[ok] nothing changed, the bot keeps the old code"; fi ;;
  undo) restore; restart ;;
  *) echo "usage: ./upd.sh begin | done | undo" ;;
esac
