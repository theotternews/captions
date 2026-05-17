"""Disk cache for Ably session tokens (publisher + subscriber).

Cache files live at .captions/sessions/<safe_name>.json under the caller's
working directory, where <safe_name> replaces ':' with '_' in the channel name.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ably.types.tokendetails import TokenDetails

_EXPIRY_BUFFER_MS = 60_000  # 1-minute safety margin


def _safe_name(channel: str) -> str:
    """Convert a channel name to a safe filename component."""
    return channel.replace(":", "_").replace("/", "_")


def cache_dir(cwd: str | Path | None = None) -> Path:
    root = Path(cwd) if cwd else Path.cwd()
    return root / ".captions" / "sessions"


def cache_path(channel: str, cwd: str | Path | None = None) -> Path:
    return cache_dir(cwd) / f"{_safe_name(channel)}.json"


def save_session(
    channel: str,
    pub: TokenDetails,
    sub: TokenDetails,
    cwd: str | Path | None = None,
) -> None:
    """Persist publisher and subscriber tokens for *channel* to disk."""
    path = cache_path(channel, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel": channel,
        "publisher_token": pub.token,
        "subscriber_token": sub.token,
        "expires_ms": pub.expires,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_session(channel: str, cwd: str | Path | None = None) -> dict | None:
    """Load cached session for *channel*; return ``None`` if the file is absent."""
    path = cache_path(channel, cwd)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def is_valid(session: dict) -> bool:
    """Return True if the cached session has at least 1 minute of life remaining."""
    expires_ms = session.get("expires_ms")
    if not expires_ms:
        return False
    now_ms = time.time() * 1000
    return float(expires_ms) > now_ms + _EXPIRY_BUFFER_MS
