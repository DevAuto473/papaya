"""OpenRouter client (stdlib only). Returns the reply text and its real cost in USD."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_A = "google/gemini-3.6-flash"            # cheap, reads images
DEFAULT_B = "anthropic/claude-haiku-5.5"         # different vendor, different mistakes
DEFAULT_C = "anthropic/claude-sonnet-5.5:online" # stronger + web search: ONLY for disputed questions


class AIError(Exception):
    pass


class Reply(str):
    """The reply text, plus how many tokens it took (for the admin log)."""
    tokens: int = 0


def models() -> tuple[str, str, str]:
    return (os.environ.get("PAPAYA_MODEL_A", DEFAULT_A).strip(),
            os.environ.get("PAPAYA_MODEL_B", DEFAULT_B).strip(),
            os.environ.get("PAPAYA_MODEL_C", DEFAULT_C).strip())


def chat(model: str, messages: list, key: str | None = None, timeout: int = 150,
         max_tokens: int = 4000) -> tuple[str, float]:
    key = key or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise AIError("OPENROUTER_API_KEY missing in .env")
    body = {"model": model, "messages": messages, "temperature": 0,
            "max_tokens": max_tokens, "usage": {"include": True}}
    req = urllib.request.Request(API, json.dumps(body).encode(), {
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "papaya"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error", {}).get("message", "")[:150]
        except Exception:
            pass
        raise AIError(f"{model}: HTTP {e.code} {detail}".strip())
    except Exception as e:
        raise AIError(f"{model}: {type(e).__name__}")
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise AIError(f"{model}: empty reply")
    usage = data.get("usage") or {}
    cost = float(usage.get("cost") or 0)
    text = Reply(text)
    text.tokens = int(usage.get("total_tokens") or 0)
    return text, cost


def extract_json(text: str):
    """Models sometimes wrap JSON in ``` fences or add a sentence; take the outermost object."""
    t = re.sub(r"```(?:json)?", "", text)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in reply")
    return json.loads(t[a:b + 1])
