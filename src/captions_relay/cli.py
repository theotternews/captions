from __future__ import annotations

import asyncio
import json
import os
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
from ably.sync.util.exceptions import AblyException

from captions_relay.ably_tokens import (
    list_active_channel_names,
    mint_publisher_token,
    mint_subscriber_token,
    publish_session_end_marker,
)
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
    normalize_caption_channel,
    subscriber_index_url,
)
from captions_relay.pulse_captions import build_pulse_pipeline_command, run_pulse_caption_pipeline

PACKAGE_VERSION = "0.1.0"


def _default_ttl() -> int:
    return int(os.environ.get(ENV_TOKEN_TTL, "14400"))


def _channel_for_session_new(channel: str | None) -> str:
    """Default ``captions:<uuidhex>`` or a caller-provided Ably channel name."""
    raw = (channel or "").strip()
    if not raw:
        return f"captions:{uuid.uuid4().hex}"
    try:
        return normalize_caption_channel(raw)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="channel") from e


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(PACKAGE_VERSION, "--version")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Live captions relay over Ably (pub/sub)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.group()
def session() -> None:
    """Manage caption sessions (create, list active channels, end)."""


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
    help="Print machine-readable JSON (channel, subscriber_url, tokens).",
)
@click.option(
    "--channel",
    type=str,
    default=None,
    help="Ably channel name; default: random captions:<uuidhex>.",
)
@click.option(
    "--pulse",
    "start_pulse",
    is_flag=True,
    help=(
        "After creating the session, start whisper pulse (same env/options as "
        "`whisper pulse` except channel and publisher token come from this command)."
    ),
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="With --pulse: echo raw whisper stdout to stderr (see whisper pulse -v).",
)
def session_new(
    ttl: int | None,
    as_json: bool,
    channel: str | None,
    start_pulse: bool,
    verbose: bool,
) -> None:
    """Generate a channel name and short-lived publisher/subscriber tokens."""
    if start_pulse and as_json:
        raise click.UsageError("Cannot combine --pulse with --json.")

    ttl_eff = ttl if ttl is not None else _default_ttl()
    channel = _channel_for_session_new(channel)
    try:
        pub = mint_publisher_token(channel, ttl_eff)
        sub = mint_subscriber_token(channel, ttl_eff)
    except AblyException as e:
        raise click.ClickException(f"Ably error: {e}") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        sub_url = subscriber_index_url(channel)
        payload = {
            "channel": channel,
            "caption_event": CAPTION_EVENT,
            "ttl_seconds": ttl_eff,
            "subscriber_url": sub_url,
            "publisher_token": pub.token,
            "subscriber_token": sub.token,
        }
        click.echo(json.dumps(payload, indent=2))
        return

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
    click.echo("Subscriber URL:")
    click.echo(f"  {subscriber_index_url(channel)}")

    if start_pulse:
        click.echo("")
        click.echo(click.style("Starting whisper pulse…", bold=True))
        _run_whisper_pulse(
            channel,
            pub.token,
            whisper_cpp_home=os.environ.get(ENV_WHISPER_CPP_HOME),
            whisper_binary=os.environ.get(ENV_WHISPER_BINARY),
            model_path=os.environ.get(ENV_WHISPER_MODEL),
            verbose=verbose,
        )


@session.command("delete")
@click.argument("channel")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate channel and print the payload; do not publish.",
)
def session_delete(channel: str, dry_run: bool) -> None:
    """Publish a session-end marker so subscribers stop (``ended: true`` on the caption event).

    Ably does not delete channel resources; existing tokens work until they expire. Requires a key
    with **publish** on this channel (your facilitator root key).
    """
    try:
        ch = normalize_caption_channel(channel)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="channel") from e

    body = {
        "t": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "text": "",
        "kind": "final",
        "ended": True,
    }
    if dry_run:
        click.echo(json.dumps({"channel": ch, "event": CAPTION_EVENT, "data": body}, indent=2))
        click.echo("(dry run — nothing published)")
        return

    try:
        publish_session_end_marker(ch)
    except AblyException as e:
        raise click.ClickException(f"Ably error: {e}") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Published session-end marker on {ch!r} (event {CAPTION_EVENT!r}).")
    click.echo(
        click.style(
            "Tokens already issued for this channel remain valid until TTL. "
            'Subscribers should disconnect; updated web subscriber shows "Session ended by host".',
            fg="yellow",
        )
    )


@session.command("list")
@click.option(
    "--prefix",
    type=str,
    default=None,
    help="Only channels whose names start with this prefix (e.g. captions:).",
)
@click.option(
    "--limit",
    type=int,
    default=100,
    show_default=True,
    help="Page size (Ably allows up to 1000).",
)
@click.option(
    "--all-pages",
    "fetch_all_pages",
    is_flag=True,
    help="Follow pagination until there is no next page (capped internally).",
)
@click.option("--json", "as_json", is_flag=True, help="Print a JSON array of channel names.")
def session_list(
    prefix: str | None,
    limit: int,
    fetch_all_pages: bool,
    as_json: bool,
) -> None:
    """List **active** Ably channels (must have been in use recently).

    Needs **channel-metadata** on ``*`` — use your app root key unless a restricted key includes that scope.
    """
    if limit < 1 or limit > 1000:
        raise click.BadParameter("limit must be 1–1000", param_hint="limit")
    try:
        names = list_active_channel_names(
            limit=limit,
            prefix=prefix,
            fetch_all_pages=fetch_all_pages,
        )
    except AblyException as e:
        raise click.ClickException(f"Ably error: {e}") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps(names, indent=2))
        return

    if not names:
        click.echo("(no active channels in this page; try --all-pages or a different --prefix)")
        return
    for n in names:
        click.echo(n)


@main.group()
def tokens() -> None:
    """Mint tokens for an Ably channel."""


def _ttl_option():
    return click.option(
        "--ttl",
        type=int,
        default=None,
        help=f"Lifetime in seconds (default: {ENV_TOKEN_TTL} or 14400).",
    )


def _channel_arg() -> callable:
    return click.argument("channel")


@tokens.command("publisher")
@_channel_arg()
@_ttl_option()
def tokens_publisher(channel: str, ttl: int | None) -> None:
    """Emit a publisher token (stdout only)."""
    ttl_eff = ttl if ttl is not None else _default_ttl()
    try:
        token = mint_publisher_token(channel.strip(), ttl_eff).token
    except AblyException as e:
        raise click.ClickException(f"Ably error: {e}") from e
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    click.echo(token)


@tokens.command("subscriber")
@_channel_arg()
@_ttl_option()
def tokens_subscriber(channel: str, ttl: int | None) -> None:
    """Emit a subscriber token (stdout only)."""
    ttl_eff = ttl if ttl is not None else _default_ttl()
    try:
        token = mint_subscriber_token(channel.strip(), ttl_eff).token
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


def _normalize_cli_channel(channel: str, *, param_hint: str) -> str:
    try:
        return normalize_caption_channel(channel)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint=param_hint) from e


def _run_whisper_pulse(
    channel: str,
    publisher_token: str,
    *,
    ffmpeg_bin: str = "ffmpeg",
    pulse_device: str = "whisper_sink.monitor",
    sample_rate: int = 16000,
    pcm_format: str = "s16",
    step_ms: int = 1000,
    length_ms: int = 10000,
    keep_ms: int = 500,
    whisper_cpp_home: str | None = None,
    whisper_binary: str | None = None,
    model_path: str | None = None,
    extra_whisper_args: str = "",
    line_kind: str = "auto",
    debounce_ms: int = 400,
    min_interval_ms: int = 450,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Shared implementation for ``whisper pulse`` and ``session new --pulse``."""
    ch = _normalize_cli_channel(channel, param_hint="channel")

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

    async def _run() -> int:
        return await run_pulse_caption_pipeline(
            channel=ch,
            publisher_token=tok,
            shell_command=shell_cmd,
            line_kind=line_kind,
            debounce_ms=debounce_ms,
            min_interval_ms=min_interval_ms,
            quiet_ably_logs=True,
            verbose_echo_whisper=verbose,
        )

    try:
        exit_code = asyncio.run(_run())
    except KeyboardInterrupt:
        click.echo("", err=True)
        raise SystemExit(130) from None
    raise SystemExit(exit_code)


@main.group()
def whisper() -> None:
    """Stream local whisper transcript lines to Ably (publisher)."""


@whisper.command("pulse")
@click.option(
    "--channel",
    type=str,
    required=True,
    help="Ably channel name.",
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
    type=click.Choice(["auto", "final", "partial"]),
    default="auto",
    show_default=True,
    help=(
        "auto: interim \\r rewrites publish as partial, completed \\n lines as final. "
        "final/partial: only \\n-terminated lines emit, fixed kind (legacy)."
    ),
)
@click.option("--debounce-ms", type=int, default=400, show_default=True)
@click.option("--min-interval-ms", type=int, default=450, show_default=True)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the ffmpeg | whisper shell command and exit (no Ably, no subprocess).",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Echo whisper's raw stdout bytes to stderr (no log formatting; Ably log level unchanged but defaults stay quiet).",
)
def whisper_pulse(
    channel: str,
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
    _run_whisper_pulse(
        channel,
        publisher_token or "",
        ffmpeg_bin=ffmpeg_bin,
        pulse_device=pulse_device,
        sample_rate=sample_rate,
        pcm_format=pcm_format,
        step_ms=step_ms,
        length_ms=length_ms,
        keep_ms=keep_ms,
        whisper_cpp_home=whisper_cpp_home,
        whisper_binary=whisper_binary,
        model_path=model_path,
        extra_whisper_args=extra_whisper_args,
        line_kind=line_kind,
        debounce_ms=debounce_ms,
        min_interval_ms=min_interval_ms,
        dry_run=dry_run,
        verbose=verbose,
    )
