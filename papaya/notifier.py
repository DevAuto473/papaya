"""Telegram (and console) senders."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class ConsoleNotifier:
    def send(self, chat_id, text: str, markup=None, silent: bool = False) -> bool:
        print(f"[→ {chat_id}]\n{text}\n")
        return True


class TelegramNotifier:
    def __init__(self, token: str, on_sent=None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.url = self.base + "sendMessage"
        self.on_sent = on_sent            # callback(chat_id, message_id) -> used by /clear

    def send(self, chat_id, text: str, markup=None, silent: bool = False):
        """Returns the new message id (truthy) or False."""
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if silent:
            payload["disable_notification"] = True
        if markup:
            payload["reply_markup"] = markup
        body = json.dumps(payload).encode()
        ok, data = False, {}
        for attempt in range(3):
            req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    ok = r.status == 200
                    data = json.load(r) if ok else {}
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:          # Telegram flood limit: wait as told, then retry
                    try:
                        wait = int((json.load(e).get("parameters") or {}).get("retry_after") or 3)
                    except Exception:
                        wait = 3
                    time.sleep(min(wait, 30))
                    continue
                return False
            except urllib.error.URLError:
                if attempt < 2:                             # never reached Telegram: safe to retry
                    time.sleep(2 * (attempt + 1))
                    continue
                return False
            except Exception:
                return False   # token never printed; caller records failure and retries next cycle
        if not ok:
            return False
        mid = (data.get("result") or {}).get("message_id") or True
        if self.on_sent and mid is not True:
            try:
                self.on_sent(chat_id, mid)
            except Exception:
                pass
        return mid

    def delete(self, chat_id, ids) -> None:
        ids = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]
        for k in range(0, len(ids), 100):
            body = json.dumps({"chat_id": chat_id, "message_ids": ids[k:k + 100]}).encode()
            req = urllib.request.Request(self.base + "deleteMessages", body, {"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20).close()
            except Exception:
                pass                       # older than 48h or already gone
