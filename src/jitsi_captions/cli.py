from __future__ import annotations

import json
import os
import uuid

import click
from ably.sync.util.exceptions import AblyException

from jitsi_captions.ably_tokens import mint_publisher_token, mint_subscriber_token
from jitsi_captions.config import channel_for_session

PACKAGE_VERSION = "0.1.0"
CAPTION_EVENT = "caption"

WEB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "web"))


def _default_ttl() -> int:
    return int(os.environ.get("JITSI_CAPTIONS_TOKEN_TTL", "14400"))


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(PACKAGE_VERSION, "--version")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Jitsi companion captions relay (Ably pub/sub)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.group()
def session() -> None:
    """Create caption sessions."""


@session.command("new")
@click.option(
    "--ttl",
    type=int,
    default=None,
    help="Token lifetime in seconds (default: 14400 or JITSI_CAPTIONS_TOKEN_TTL).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print machine-readable JSON (channel, session_id, tokens).",
)
def session_new(ttl: int | None, as_json: bool) -> None:
    """Generate a new session id, channel name, and short-lived publisher/subscriber tokens."""
    ttl_eff = ttl if ttl is not None else _default_ttl()
    sid = uuid.uuid4().hex
    channel = channel_for_session(sid)
    try:
        pub = mint_publisher_token(sid, ttl_eff)
        sub = mint_subscriber_token(sid, ttl_eff)
    except AblyException as e:
        raise click.ClickException(f"Ably error: {e}") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        payload = {
            "session_id": sid,
            "channel": channel,
            "caption_event": CAPTION_EVENT,
            "ttl_seconds": ttl_eff,
            "publisher_token": pub.token,
            "subscriber_token": sub.token,
        }
        click.echo(json.dumps(payload, indent=2))
        return

    subscriber_path = "/subscriber/index.html"
    click.echo(f"session_id:  {sid}")
    click.echo(f"channel:     {channel}")
    click.echo(f"event:       {CAPTION_EVENT}")
    click.echo(f"ttl:         {ttl_eff}s (~{ttl_eff // 3600}h)" if ttl_eff >= 3600 else f"ttl:         {ttl_eff}s")
    click.echo("")
    click.echo(
        click.style(
            "Keep publisher_token PRIVATE. Paste subscriber_token into the subscriber page.",
            fg="yellow",
        )
    )
    click.echo("")
    click.echo(click.style("publisher_token:", bold=True))
    click.echo(pub.token)
    click.echo("")
    click.echo(click.style("subscriber_token:", bold=True))
    click.echo(sub.token)
    click.echo("")
    click.echo("Subscriber URL (serve web/ locally, see README):")
    click.echo(f"  http://localhost:8765{subscriber_path}?channel={channel}")
    click.echo(f"WEB_DIR={WEB_DIR}")


@main.group()
def tokens() -> None:
    """Mint tokens for an existing session."""


def _ttl_option():
    return click.option(
        "--ttl",
        type=int,
        default=None,
        help="Lifetime in seconds (default: JITSI_CAPTIONS_TOKEN_TTL or 14400).",
    )


def _session_arg() -> callable:
    return click.argument("session_id")


@tokens.command("publisher")
@_session_arg()
@_ttl_option()
def tokens_publisher(session_id: str, ttl: int | None) -> None:
    """Emit a publisher token (stdout only)."""
    ttl_eff = ttl if ttl is not None else _default_ttl()
    try:
        token = mint_publisher_token(session_id.strip(), ttl_eff).token
    except AblyException as e:
        raise click.ClickException(f"Ably error: {e}") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    click.echo(token)


@tokens.command("subscriber")
@_session_arg()
@_ttl_option()
def tokens_subscriber(session_id: str, ttl: int | None) -> None:
    """Emit a subscriber token (stdout only)."""
    ttl_eff = ttl if ttl is not None else _default_ttl()
    try:
        token = mint_subscriber_token(session_id.strip(), ttl_eff).token
    except AblyException as e:
        raise click.ClickException(f"Ably error: {e}") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    click.echo(token)
