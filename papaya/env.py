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


def add_account(name: str) -> bool:
    """Register a new device name everywhere it must be known: this process, the .env file
    (so a restart keeps it) and the PAPAYA_ACCOUNTS GitHub secret (so the scheduled Actions
    check covers it). Best-effort: returns False if the GitHub part failed, never raises."""
    import subprocess
    current = accounts()
    if name in current:
        return True
    current.append(name)
    joined = ",".join(current)
    os.environ["PAPAYA_ACCOUNTS"] = joined
    home = Path(os.environ.get("PAPAYA_HOME", ".")).resolve()
    try:
        env_path = home / ".env"
        if env_path.exists():
            lines = [l for l in env_path.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("PAPAYA_ACCOUNTS=")]
            lines.append(f"PAPAYA_ACCOUNTS={joined}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    try:
        r = subprocess.run(["gh", "secret", "set", "PAPAYA_ACCOUNTS", "--body", joined],
                           cwd=str(home), timeout=20, capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def admin_id():
    v = os.environ.get("PAPAYA_ADMIN_CHAT_ID", "").strip()
    return int(v) if v.lstrip("-").isdigit() else None
