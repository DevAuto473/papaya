"""Tiny .env loader + config (no extra dependency)."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def accounts() -> list[str]:
    return [a.strip() for a in os.environ.get("PAPAYA_ACCOUNTS", "").split(",") if a.strip()]


def admin_id():
    v = os.environ.get("PAPAYA_ADMIN_CHAT_ID", "").strip()
    return int(v) if v.lstrip("-").isdigit() else None
