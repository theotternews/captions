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
ENV_SIGNAL_ACCOUNT = "CAPTIONS_SIGNAL_ACCOUNT"
ENV_SIGNAL_CLI_BIN = "CAPTIONS_SIGNAL_CLI_BIN"
ENV_SIGNAL_ALLOWED_SENDERS = "CAPTIONS_SIGNAL_ALLOWED_SENDERS"
ENV_SIGNAL_JITSI_HOSTS = "CAPTIONS_SIGNAL_JITSI_HOSTS"
ENV_SIGNAL_ANY_JITSI_HOST = "CAPTIONS_SIGNAL_ANY_JITSI_HOST"
ENV_SIGNAL_ALLOW_SELF = "CAPTIONS_SIGNAL_ALLOW_SELF"

_DEFAULT_SIGNAL_CLI_BIN = "signal-cli"
_DEFAULT_SIGNAL_JITSI_HOSTS = ("meet.jit.si",)

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


def _normalize_phone(raw: str) -> str:
    """Trim whitespace and surrounding punctuation from a phone-number-like string."""
    return (raw or "").strip()


def signal_account() -> str:
    """Return the linked Signal account E.164 (env ``CAPTIONS_SIGNAL_ACCOUNT``)."""
    acct = _normalize_phone(os.environ.get(ENV_SIGNAL_ACCOUNT, ""))
    if not acct:
        raise ValueError(
            f"Set {ENV_SIGNAL_ACCOUNT} to the Signal account E.164 this device is linked to "
            f"(e.g. export {ENV_SIGNAL_ACCOUNT}=+15551234567)."
        )
    return acct


def signal_cli_bin() -> str:
    """Path to the ``signal-cli`` executable (env ``CAPTIONS_SIGNAL_CLI_BIN``)."""
    return os.environ.get(ENV_SIGNAL_CLI_BIN, "").strip() or _DEFAULT_SIGNAL_CLI_BIN


def signal_allowed_senders() -> set[str]:
    """Trusted sender E.164 numbers from ``CAPTIONS_SIGNAL_ALLOWED_SENDERS`` (comma-separated)."""
    raw = os.environ.get(ENV_SIGNAL_ALLOWED_SENDERS, "")
    return {p for p in (_normalize_phone(x) for x in raw.split(",")) if p}


def signal_jitsi_hosts() -> set[str]:
    """Allowed Jitsi hostnames for Signal-triggered sessions.

    From ``CAPTIONS_SIGNAL_JITSI_HOSTS`` (comma-separated, lowercased); defaults to
    ``meet.jit.si``.
    """
    raw = os.environ.get(ENV_SIGNAL_JITSI_HOSTS, "")
    hosts = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return hosts or set(_DEFAULT_SIGNAL_JITSI_HOSTS)


def validate_jitsi_url(
    url: str,
    *,
    allowed_hosts: set[str] | None = None,
    allow_any_host: bool = False,
) -> str:
    """Validate a Jitsi meeting URL for use as a trigger.

    Requires an ``https`` scheme, a host, and a non-empty path (room). Unless
    ``allow_any_host`` is set, the host must be in ``allowed_hosts`` (defaults to
    :func:`signal_jitsi_hosts`). Returns the stripped URL or raises :class:`ValueError`.
    """
    s = (url or "").strip()
    if not s:
        raise ValueError("Jitsi URL must be non-empty.")
    parsed = urlparse(s)
    if parsed.scheme != "https":
        raise ValueError("Jitsi URL must use https.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Jitsi URL must include a host.")
    if not allow_any_host:
        hosts = allowed_hosts if allowed_hosts is not None else signal_jitsi_hosts()
        if host not in hosts:
            raise ValueError(
                f"Jitsi host {host!r} is not allowed (allowed: {', '.join(sorted(hosts)) or 'none'})."
            )
    if not parsed.path.strip("/"):
        raise ValueError("Jitsi URL must include a room name in the path.")
    return s

