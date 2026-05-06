"""Mint Ably token details for caption channels (sync REST)."""

from __future__ import annotations

from ably.sync.rest.rest import AblyRestSync
from ably.types.tokendetails import TokenDetails

from captions_relay.config import channel_for_session, get_ably_api_key


def _client() -> AblyRestSync:
    return AblyRestSync(key=get_ably_api_key())


def mint_publisher_token(session_id: str, ttl_seconds: int) -> TokenDetails:
    """Subscribe + publish on the session channel."""
    channel = channel_for_session(session_id)
    return _client().auth.request_token(
        {
            "capability": {channel: ["publish", "subscribe"]},
            "ttl": ttl_seconds * 1000,
        },
    )


def mint_subscriber_token(session_id: str, ttl_seconds: int) -> TokenDetails:
    """Subscribe only on the session channel."""
    channel = channel_for_session(session_id)
    return _client().auth.request_token(
        {
            "capability": {channel: ["subscribe"]},
            "ttl": ttl_seconds * 1000,
        },
    )
