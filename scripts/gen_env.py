"""Create an ignored .env with a random local database password (idempotent)."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env = root / ".env"
if env.exists():
    print(f".env already exists at {env}; leaving it unchanged")
    sys.exit(0)
pw = secrets.token_urlsafe(24)
text = (root / ".env.example").read_text().replace("change-me-local-only", pw)
env.write_text(text)
env.chmod(0o600)
print(f"wrote {env} (random local DB password, mode 600)")
