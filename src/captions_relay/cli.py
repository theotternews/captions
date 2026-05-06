from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import uuid
from pathlib import Path

import click
from ably.sync.util.exceptions import AblyException

from captions_relay.ably_tokens import mint_publisher_token, mint_subscriber_token
from captions_relay.config import (
    CAPTION_EVENT,
    ENV_PUBLISHER_TOKEN,
    ENV_TOKEN_TTL,
    ENV_WHISPER_BINARY,
    ENV_WHISPER_CPP_HOME,
    ENV_WHISPER_MODEL,
    WHISPER_CPP_DEFAULT_MODEL,
    WHISPER_CPP_REL_BINARY,
    WHISPER_CPP_REL_MODELS_DIR,
    channel_for_session,
    normalize_caption_channel,
)
from captions_relay.pulse_captions import build_pulse_pipeline_command, run_pulse_caption_pipeline

PACKAGE_VERSION = "0.1.0"

WEB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "web"))


def _default_ttl() -> int:
    return int(os.environ.get(ENV_TOKEN_TTL, "14400"))


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(PACKAGE_VERSION, "--version")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Live captions relay over Ably (pub/sub)."""
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
    help=f"Token lifetime in seconds (default: 14400 or {ENV_TOKEN_TTL}).",
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
        help=f"Lifetime in seconds (default: {ENV_TOKEN_TTL} or 14400).",
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


def _resolve_whisper_paths_from_home(
    whisper_cpp_home: str | None,
    whisper_binary: str | None,
    model_path: str | None,
) -> tuple[str, str]:
    """Apply WHISPER_CPP_HOME defaults: build/bin/whisper-stream-pcm and models/."""
    home = (whisper_cpp_home or "").strip()
    wb = (whisper_binary or "").strip()
    mp = (model_path or "").strip()

    if not home:
        return wb, mp

    root = Path(home).expanduser().resolve()
    bin_default = str(root.joinpath(*WHISPER_CPP_REL_BINARY))

    if not wb:
        wb = bin_default

    if not mp:
        mp = str(root / WHISPER_CPP_REL_MODELS_DIR / WHISPER_CPP_DEFAULT_MODEL)
    else:
        p = Path(mp).expanduser()
        if p.is_absolute():
            mp = str(p)
        elif "/" not in mp.replace("\\", "/"):
            mp = str(root / WHISPER_CPP_REL_MODELS_DIR / mp)
        else:
            mp = str(Path(mp).expanduser().resolve())

    return wb, mp


def _resolve_publisher_channel(session_id: str | None, channel: str | None) -> str:
    if session_id and channel:
        raise click.UsageError("Use only one of --session-id or --channel.")
    if session_id:
        try:
            return channel_for_session(session_id)
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint="session_id") from e
    if channel:
        try:
            return normalize_caption_channel(channel)
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint="channel") from e
    raise click.UsageError("Provide --session-id or --channel.")


@main.group()
def whisper() -> None:
    """Stream local whisper transcript lines to Ably (publisher)."""


@whisper.command("pulse")
@click.option(
    "--session-id",
    type=str,
    default=None,
    help="Session id from session new (alternative to --channel).",
)
@click.option(
    "--channel",
    type=str,
    default=None,
    help="captions:<hex> channel string (alternative to --session-id).",
)
@click.option(
    "--publisher-token",
    type=str,
    default=None,
    envvar=ENV_PUBLISHER_TOKEN,
    help=f"Publisher token (or env {ENV_PUBLISHER_TOKEN}).",
)
@click.option("--ffmpeg", "ffmpeg_bin", default="ffmpeg", show_default=True, help="ffmpeg executable.")
@click.option(
    "--pulse-device",
    default="whisper_sink.monitor",
    show_default=True,
    help="Pulse source device for ffmpeg -i.",
)
@click.option("--sample-rate", type=int, default=16000, show_default=True)
@click.option("--pcm-format", "pcm_format", default="s16", show_default=True)
@click.option("--step", "step_ms", type=int, default=1000, show_default=True)
@click.option("--length", "length_ms", type=int, default=10000, show_default=True)
@click.option("--keep", "keep_ms", type=int, default=500, show_default=True)
@click.option(
    "--whisper-cpp-home",
    type=str,
    default=None,
    envvar=ENV_WHISPER_CPP_HOME,
    help=(
        "whisper.cpp checkout; defaults binary to build/bin/whisper-stream-pcm "
        f"and model to models/{WHISPER_CPP_DEFAULT_MODEL} under this path "
        f"(env {ENV_WHISPER_CPP_HOME})."
    ),
)
@click.option(
    "--whisper-binary",
    type=str,
    default=None,
    envvar=ENV_WHISPER_BINARY,
    help=(
        "path to whisper-stream-pcm (overrides home default; "
        f"or env {ENV_WHISPER_BINARY})."
    ),
)
@click.option(
    "--model",
    "-m",
    "model_path",
    type=str,
    default=None,
    envvar=ENV_WHISPER_MODEL,
    help=(
        "ggml model path or filename under models/ when --whisper-cpp-home is set "
        f"(or env {ENV_WHISPER_MODEL})."
    ),
)
@click.option(
    "--extra-whisper-args",
    type=str,
    default="",
    help="Extra whisper-stream-pcm arguments as one string (parsed with shlex.split).",
)
@click.option(
    "--line-kind",
    type=click.Choice(["final", "partial"]),
    default="final",
    show_default=True,
    help="Whether each stdout line is published as a final or partial caption.",
)
@click.option("--debounce-ms", type=int, default=400, show_default=True)
@click.option("--min-interval-ms", type=int, default=450, show_default=True)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the ffmpeg | whisper shell command and exit (no Ably, no subprocess).",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable INFO logging.")
def whisper_pulse(
    session_id: str | None,
    channel: str | None,
    publisher_token: str | None,
    ffmpeg_bin: str,
    pulse_device: str,
    sample_rate: int,
    pcm_format: str,
    step_ms: int,
    length_ms: int,
    keep_ms: int,
    whisper_cpp_home: str | None,
    whisper_binary: str | None,
    model_path: str | None,
    extra_whisper_args: str,
    line_kind: str,
    debounce_ms: int,
    min_interval_ms: int,
    dry_run: bool,
    verbose: bool,
) -> None:
    """PulseAudio → ffmpeg (WAV) → whisper-stream-pcm; publish stdout lines to Ably."""
    ch = _resolve_publisher_channel(session_id, channel)

    wb, mp = _resolve_whisper_paths_from_home(whisper_cpp_home, whisper_binary, model_path)

    if not wb:
        raise click.UsageError(
            f"Set --whisper-cpp-home / {ENV_WHISPER_CPP_HOME}, "
            f"or pass --whisper-binary, or set {ENV_WHISPER_BINARY}."
        )

    if not mp:
        raise click.UsageError(
            f"Set --whisper-cpp-home / {ENV_WHISPER_CPP_HOME}, "
            f"or pass --model / -m, or set {ENV_WHISPER_MODEL}."
        )

    tok = (publisher_token or "").strip()
    if not tok and not dry_run:
        raise click.UsageError(f"Pass --publisher-token or set {ENV_PUBLISHER_TOKEN}.")

    try:
        extras = shlex.split(extra_whisper_args) if extra_whisper_args.strip() else []
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="extra-whisper-args") from e

    shell_cmd = build_pulse_pipeline_command(
        ffmpeg_bin=ffmpeg_bin,
        pulse_device=pulse_device,
        sample_rate=sample_rate,
        whisper_bin=wb,
        model_path=mp,
        pcm_format=pcm_format,
        step_ms=step_ms,
        length_ms=length_ms,
        keep_ms=keep_ms,
        extra_whisper_args=extras,
    )

    if dry_run:
        click.echo(shell_cmd)
        return

    if verbose:
        logging.basicConfig(level=logging.INFO)

    async def _run() -> None:
        await run_pulse_caption_pipeline(
            channel=ch,
            publisher_token=tok,
            shell_command=shell_cmd,
            line_kind=line_kind,
            debounce_ms=debounce_ms,
            min_interval_ms=min_interval_ms,
            quiet_ably_logs=not verbose,
        )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("", err=True)
