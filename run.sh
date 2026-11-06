#!/usr/bin/env bash
# Keeps papaya running: starts it, and restarts it 10 seconds after any stop or crash.
cd "$HOME/papaya" || exit 1
exec 9>"$HOME/papaya/.run.lock"
flock -n 9 || { echo "papaya is already running"; exit 0; }
mkdir -p logs
while true; do
  if [ -f logs/bot.log ] && [ "$(stat -c%s logs/bot.log)" -gt 5000000 ]; then mv -f logs/bot.log logs/bot.old.log; fi
  echo "=== start $(date '+%F %T')" >> logs/bot.log
  .venv/bin/python -u -m papaya.main run >> logs/bot.log 2>&1
  code=$?
  echo "=== stopped $(date '+%F %T') (code $code), restarting in 10s" >> logs/bot.log
  sleep 10
done
