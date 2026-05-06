"""Environment and shared settings."""

import os
import re

ENV_API_KEY = "JITSI_CAPTIONS_ABLY_API_KEY"


def get_ably_api_key() -> str:
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        raise ValueError(
            f"Set {ENV_API_KEY} to your Ably root API key (e.g. export {ENV_API_KEY}=xxxxx:yyyyy)."
        )
    return key


def channel_for_session(session_id: str) -> str:
    sid = session_id.strip()
    if not sid:
        raise ValueError("session id must be non-empty")
    if ":" in sid:
        raise ValueError("session id must not contain ':'; use bare UUID or hex id")
    # Capability keys match the literal channel string. Normalize dashed UUID forms to
    # the same lowercase 32‑hex slug as session new (uuid.uuid4().hex).
    condensed = "".join(sid.lower().split("-"))
    if not condensed or not re.fullmatch(r"[0-9a-f]+", condensed):
        raise ValueError("session id must be hexadecimal only (hyphens OK for UUIDs)")
    if condensed == "cap":
        raise ValueError(
            "\"cap\" is not a session id — use the hex from `session new` (typically 32 characters)."
        )
    return f"captions:{condensed}"
