"""Mint Ably token details for caption channels (sync REST)."""

from __future__ import annotations

from datetime import datetime, timezone

import ably
from ably.sync.rest.rest import AblyRestSync
from ably.sync.util.exceptions import AblyException
from ably.types.tokendetails import TokenDetails

from captions_relay.config import CAPTION_EVENT, get_ably_api_key, normalize_caption_channel


def _client() -> AblyRestSync:
    return AblyRestSync(key=get_ably_api_key())


def _channel_item_to_name(item: object) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        v = item.get("name")
        if v is not None:
            return str(v)
        v = item.get("channelId")
        if v is not None:
            return str(v)
    return None


def list_active_channel_names(
    *,
    limit: int = 100,
    prefix: str | None = None,
    fetch_all_pages: bool = False,
    max_pages: int = 100,
) -> list[str]:
    """Return currently **active** channel names (Ably `GET /channels`, ``by=id``).

    Requires API key **channel-metadata** on resource ``*`` (typical root keys).
    Enumeration is rate-limited; prefer a modest ``limit`` and avoid ``fetch_all_pages`` unless needed.
    """
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    client = _client()
    params: dict[str, str] = {"limit": str(limit), "by": "id"}
    if (prefix or "").strip():
        params["prefix"] = prefix.strip()

    page = client.request("GET", "/channels", version=ably.api_version, params=params)
    if not page.success:
        AblyException.raise_for_response(page.response)

    names: list[str] = []
    page_count = 0
    while page is not None and page_count < max_pages:
        for item in page.items:
            n = _channel_item_to_name(item)
            if n:
                names.append(n)
        page_count += 1
        if not fetch_all_pages or not page.has_next():
            break
        page = page.next()
        if page is None:
            break
        if not page.success:
            AblyException.raise_for_response(page.response)

    return names


def mint_publisher_token(channel: str, ttl_seconds: int) -> TokenDetails:
    """Subscribe + publish on the given channel."""
    ch = normalize_caption_channel(channel)
    return _client().auth.request_token(
        {
            "capability": {ch: ["publish", "subscribe"]},
            "ttl": ttl_seconds * 1000,
        },
    )


def mint_subscriber_token(channel: str, ttl_seconds: int) -> TokenDetails:
    """Subscribe only on the given channel."""
    ch = normalize_caption_channel(channel)
    return _client().auth.request_token(
        {
            "capability": {ch: ["subscribe"]},
            "ttl": ttl_seconds * 1000,
        },
    )


def publish_session_end_marker(channel: str) -> None:
    """Publish a final ``caption`` message with ``ended: true`` (subscribers should disconnect)."""
    ch = normalize_caption_channel(channel)
    body = {
        "t": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "text": "",
        "kind": "final",
        "ended": True,
    }
    _client().channels.get(ch).publish(CAPTION_EVENT, body)
