"""Environment and shared settings."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ENV_API_KEY = "CAPTIONS_ABLY_API_KEY"
ENV_PUBLISHER_TOKEN = "CAPTIONS_PUBLISHER_TOKEN"
ENV_TOKEN_TTL = "CAPTIONS_TOKEN_TTL"
ENV_WHISPER_BINARY = "CAPTIONS_WHISPER_STREAM_PCM"
ENV_WHISPER_MODEL = "CAPTIONS_WHISPER_MODEL"
ENV_WHISPER_CPP_HOME = "WHISPER_CPP_HOME"
ENV_SUBSCRIBER_PAGES_BASE = "CAPTIONS_SUBSCRIBER_PAGES_BASE"
ENV_NODE_BIN = "CAPTIONS_NODE_BIN"
ENV_JITSI_PULLER_SCRIPT = "CAPTIONS_JITSI_PULLER_SCRIPT"

# jitsi-audio-puller lives at <project-root>/jitsi-audio-puller/index.js;
# __file__ is src/captions_relay/config.py so .parent*3 is the project root.
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_JITSI_PULLER_SCRIPT = str(_PROJECT_ROOT / "jitsi-audio-puller" / "index.js")


def default_jitsi_puller_script() -> str:
    """Return the path to jitsi-audio-puller index.js, overridable via env."""
    return os.environ.get(ENV_JITSI_PULLER_SCRIPT, "").strip() or _DEFAULT_JITSI_PULLER_SCRIPT

CAPTION_EVENT = "caption"

WHISPER_CPP_REL_BINARY = ("build", "bin", "whisper-stream-pcm")
WHISPER_CPP_REL_MODELS_DIR = "models"
WHISPER_CPP_DEFAULT_MODEL = "ggml-large-v3-turbo-q8_0.bin"

# Ably: non-empty, no newlines, must not start with '[' or ':', namespace (before
# first ':') must not contain '*'; practical URL length.
_MAX_CHANNEL_LEN = 2048


def get_ably_api_key() -> str:
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        raise ValueError(
            f"Set {ENV_API_KEY} to your Ably root API key (e.g. export {ENV_API_KEY}=xxxxx:yyyyy)."
        )
    return key


_DEFAULT_SUBSCRIBER_PAGES_BASE = "https://theotternews.github.io/captions"


def subscriber_pages_base_url() -> str:
    """Root URL of the static subscriber site (GitHub Pages from ``/docs`` by default)."""
    raw = (
        os.environ.get(ENV_SUBSCRIBER_PAGES_BASE) or _DEFAULT_SUBSCRIBER_PAGES_BASE
    ).strip()
    return raw.rstrip("/")


def subscriber_index_url(channel: str) -> str:
    """Full subscriber page URL with ``channel`` query (percent-encoded)."""
    q = quote(channel, safe="")
    return f"{subscriber_pages_base_url()}/subscriber/index.html?channel={q}"


def validate_ably_channel_name(name: str) -> str:
    """Check Ably channel naming rules (see https://ably.com/docs/channels )."""
    channel = (name or "").strip()
    if not channel:
        raise ValueError("Channel name must be non-empty.")
    if "\n" in channel or "\r" in channel:
        raise ValueError("Channel name must not contain newline characters.")
    if channel.startswith("[") or channel.startswith(":"):
        raise ValueError("Channel name must not start with '[' or ':'.")
    if len(channel) > _MAX_CHANNEL_LEN:
        raise ValueError(f"Channel name must be at most {_MAX_CHANNEL_LEN} characters.")
    ns = channel.split(":", 1)[0]
    if "*" in ns:
        raise ValueError("Channel namespace (before the first ':') must not contain '*'.")
    return channel


def _extract_channel_from_input(raw: str) -> str:
    """Pull channel=… out of pasted subscriber URLs; otherwise strip."""
    s = (raw or "").strip()
    if not s:
        return s
    lower = s.lower()
    if "channel=" in lower or "channel%3d" in lower:
        if lower.startswith("http://") or lower.startswith("https://"):
            q = parse_qs(urlparse(s).query)
            vals = q.get("channel") or q.get("CHANNEL")
            if vals and vals[0].strip():
                return unquote(vals[0]).strip()
        m = re.search(r"(?i)[#?&]channel=([^&#]+)", s)
        if m:
            return unquote(m.group(1)).strip()
    return s


def normalize_caption_channel(raw: str) -> str:
    """Extract ``channel`` from a pasted URL if needed, then validate as an Ably channel name."""
    s = _extract_channel_from_input(raw)
    if not s:
        raise ValueError("Channel name must be non-empty.")
    return validate_ably_channel_name(s)

