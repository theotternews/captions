"""Environment and shared settings."""

import os
import re

ENV_API_KEY = "CAPTIONS_ABLY_API_KEY"
ENV_PUBLISHER_TOKEN = "CAPTIONS_PUBLISHER_TOKEN"
ENV_TOKEN_TTL = "CAPTIONS_TOKEN_TTL"
ENV_WHISPER_BINARY = "CAPTIONS_WHISPER_STREAM_PCM"
ENV_WHISPER_MODEL = "CAPTIONS_WHISPER_MODEL"
ENV_WHISPER_CPP_HOME = "WHISPER_CPP_HOME"

CAPTION_EVENT = "caption"

WHISPER_CPP_REL_BINARY = ("build", "bin", "whisper-stream-pcm")
WHISPER_CPP_REL_MODELS_DIR = "models"
WHISPER_CPP_DEFAULT_MODEL = "ggml-base.bin"


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


def normalize_caption_channel(raw: str) -> str:
    """Normalize `captions:<hex>` the same way as `web/subscriber` and `glue.js`."""
    s = (raw or "").strip()
    idx = s.find("captions:")
    if idx > 0:
        s = s[idx:]
    m = re.fullmatch(r"captions:([a-fA-F0-9\-]+)", s)
    if not m:
        raise ValueError(
            "Channel must be captions:<session_hex> from `session new` "
            "(hex only after the colon; optional hyphens)."
        )
    slug = m.group(1).replace("-", "").lower()
    if not slug or not re.fullmatch(r"[0-9a-f]+", slug):
        raise ValueError("Channel slug after captions: must be hexadecimal.")
    if slug == "cap":
        raise ValueError(
            '"captions:cap" is not valid — use the full captions:… channel from `session new`.'
        )
    return f"captions:{slug}"
