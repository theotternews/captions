from __future__ import annotations

import asyncio
import json
import os
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
    ENV_NODE_BIN,
    ENV_JITSI_PULLER_SCRIPT,
    ENV_PUBLISHER_TOKEN,
    ENV_SIGNAL_ACCOUNT,
    ENV_SIGNAL_ALLOW_SELF,
    ENV_SIGNAL_ALLOWED_SENDERS,
    ENV_SIGNAL_ANY_JITSI_HOST,
    ENV_SIGNAL_CLI_BIN,
    ENV_SIGNAL_JITSI_HOSTS,
    ENV_TOKEN_TTL,
    ENV_WHISPER_BINARY,
    ENV_WHISPER_CPP_HOME,
    ENV_WHISPER_MODEL,
    WHISPER_CPP_DEFAULT_MODEL,
    WHISPER_CPP_REL_BINARY,
    WHISPER_CPP_REL_MODELS_DIR,
    default_jitsi_puller_script,
    normalize_caption_channel,
    signal_allowed_senders,
    signal_cli_bin as default_signal_cli_bin,
    signal_jitsi_hosts,
    subscriber_index_url,
)
from captions_relay import session_cache
from captions_relay.pulse_captions import (
    build_jitsi_pipeline_command,
    build_pulse_pipeline_command,
    run_jitsi_caption_pipeline,
    run_jitsi_per_speaker_caption_pipeline,
    run_pulse_caption_pipeline,
)

PACKAGE_VERSION = "0.1.0"


def _default_ttl() -> int:
    return int(os.environ.get(ENV_TOKEN_TTL, "14400"))


def _room_name_from_jitsi_url(url: str) -> str | None:
    """Extract the lowercased room name from a Jitsi meeting URL, or return ``None``."""
    try:
        parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
        if not parts:
            return None
        # meet.jit.si/moderated/<hash> — MUC room is the hash, not "moderated/<hash>".
        if parts[0] == "moderated" and len(parts) >= 2:
            return parts[-1].lower()
        return "/".join(parts).lower()
    except Exception:
        return None


def _channel_for_session_new(channel: str | None, *, jitsi_url: str | None = None) -> str:
    """Default channel name: Jitsi room → ``captions:<room>``, else ``captions:<uuidhex>``.

    An explicit ``--channel`` always takes precedence.
    """
    raw = (channel or "").strip()
    if not raw and jitsi_url:
        room = _room_name_from_jitsi_url(jitsi_url)
        if room:
            raw = f"captions:{room}"
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
    help=(
        "With --pulse: per-speaker Jitsi mode echoes captioned lines (Name: text) to stdout; "
        "otherwise echoes raw whisper stdout to stderr (see whisper pulse -v)."
    ),
)
@click.option(
    "--whisper-cpp-home",
    type=str,
    default=None,
    envvar=ENV_WHISPER_CPP_HOME,
    help=(
        "With --pulse: whisper.cpp root (same as whisper pulse; "
        f"env {ENV_WHISPER_CPP_HOME})."
    ),
)
@click.option(
    "--whisper-binary",
    type=str,
    default=None,
    envvar=ENV_WHISPER_BINARY,
    help=f"With --pulse: whisper-stream-pcm path (env {ENV_WHISPER_BINARY}).",
)
@click.option(
    "--model",
    "-m",
    "model_path",
    type=str,
    default=None,
    envvar=ENV_WHISPER_MODEL,
    help=(
        "With --pulse/--jitsi: ggml model path or filename under models/ "
        f"(same as whisper pulse; env {ENV_WHISPER_MODEL})."
    ),
)
@click.option(
    "--jitsi",
    "jitsi_url",
    type=str,
    default=None,
    help=(
        "Jitsi meeting URL (e.g. https://meet.jit.si/MyRoom). "
        "Joins the meeting as a headless audio bot and streams audio through ffmpeg "
        "into whisper-stream-pcm. Cannot be combined with --pulse or --json."
    ),
)
@click.option(
    "--node-bin",
    type=str,
    default=None,
    envvar=ENV_NODE_BIN,
    help=f"With --jitsi: node executable (env {ENV_NODE_BIN}, default: node).",
)
@click.option(
    "--jitsi-puller-script",
    type=str,
    default=None,
    envvar=ENV_JITSI_PULLER_SCRIPT,
    help=(
        f"With --jitsi: path to jitsi-audio-puller index.js "
        f"(env {ENV_JITSI_PULLER_SCRIPT}, default: <project-root>/jitsi-audio-puller/index.js)."
    ),
)
@click.option(
    "--reconnect",
    "reconnect",
    is_flag=True,
    help=(
        "Reuse cached tokens for the session derived from --jitsi instead of minting new ones. "
        "Errors out if no valid cached session exists. Cannot be combined with --channel or --json."
    ),
)
@click.option(
    "--mixed",
    "jitsi_mixed",
    is_flag=True,
    help="With --jitsi: sum all participants into one whisper stream (legacy mode).",
)
@click.option(
    "--max-speakers",
    type=int,
    default=8,
    show_default=True,
    help="With --jitsi (default per-speaker mode): max concurrent whisper instances.",
)
def session_new(
    ttl: int | None,
    as_json: bool,
    channel: str | None,
    start_pulse: bool,
    verbose: bool,
    whisper_cpp_home: str | None,
    whisper_binary: str | None,
    model_path: str | None,
    jitsi_url: str | None,
    node_bin: str | None,
    jitsi_puller_script: str | None,
    reconnect: bool,
    jitsi_mixed: bool,
    max_speakers: int,
) -> None:
    """Generate a channel name and short-lived publisher/subscriber tokens."""
    if start_pulse and as_json:
        raise click.UsageError("Cannot combine --pulse with --json.")
    if jitsi_url and as_json:
        raise click.UsageError("Cannot combine --jitsi with --json.")
    if jitsi_url and start_pulse:
        raise click.UsageError("Cannot combine --jitsi with --pulse.")
    if reconnect and as_json:
        raise click.UsageError("Cannot combine --reconnect with --json.")
    if reconnect and channel:
        raise click.UsageError("Cannot combine --reconnect with --channel.")
    if reconnect and not jitsi_url:
        raise click.UsageError("--reconnect requires --jitsi.")

    ttl_eff = ttl if ttl is not None else _default_ttl()
    channel = _channel_for_session_new(channel, jitsi_url=jitsi_url)

    if reconnect:
        cached = session_cache.load_session(channel)
        if cached is None or not session_cache.is_valid(cached):
            raise click.ClickException(
                f"No valid cached session for channel {channel!r}. "
                "Run without --reconnect to mint fresh tokens and cache them."
            )
        pub_token = cached["publisher_token"]
        click.echo(click.style(f"Reconnecting to existing session on channel {channel!r}.", bold=True))
    else:
        try:
            pub = mint_publisher_token(channel, ttl_eff)
            sub = mint_subscriber_token(channel, ttl_eff)
        except AblyException as e:
            raise click.ClickException(f"Ably error: {e}") from e
        except ValueError as e:
            raise click.ClickException(str(e)) from e

        session_cache.save_session(channel, pub, sub)
        pub_token = pub.token

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
            pub_token,
            whisper_cpp_home=whisper_cpp_home,
            whisper_binary=whisper_binary,
            model_path=model_path,
            verbose=verbose,
        )

    if jitsi_url:
        click.echo("")
        click.echo(click.style("Joining Jitsi meeting and starting captions…", bold=True))
        _run_whisper_jitsi(
            channel,
            pub_token,
            jitsi_url=jitsi_url,
            node_bin=node_bin or "node",
            puller_script=jitsi_puller_script or default_jitsi_puller_script(),
            whisper_cpp_home=whisper_cpp_home,
            whisper_binary=whisper_binary,
            model_path=model_path,
            verbose=verbose,
            mixed=jitsi_mixed,
            max_speakers=max_speakers,
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
    """Resolve binary and model paths under a whisper.cpp root.

    If ``whisper_cpp_home`` is unset or blank, ``./whisper.cpp`` under the current
    working directory is used unless both ``whisper_binary`` and ``model_path`` are
    set (then home is unused).
    Defaults: ``build/bin/whisper-stream-pcm`` and ``models/{WHISPER_CPP_DEFAULT_MODEL}``.
    """
    home = (whisper_cpp_home or "").strip()
    wb = (whisper_binary or "").strip()
    mp = (model_path or "").strip()

    if not home:
        if wb and mp:
            return wb, mp
        home = str((Path.cwd() / "whisper.cpp").resolve())

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
            f"Pass --whisper-binary or set {ENV_WHISPER_BINARY} "
            f"(defaults use ./whisper.cpp under $PWD or set {ENV_WHISPER_CPP_HOME})."
        )

    if not mp:
        raise click.UsageError(
            f"Pass --model / -m or set {ENV_WHISPER_MODEL} "
            f"(defaults use ./whisper.cpp under $PWD or set {ENV_WHISPER_CPP_HOME})."
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


def _run_whisper_jitsi(
    channel: str,
    publisher_token: str,
    *,
    jitsi_url: str,
    node_bin: str = "node",
    puller_script: str | None = None,
    ffmpeg_bin: str = "ffmpeg",
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
    mixed: bool = False,
    max_speakers: int = 8,
) -> None:
    """Shared implementation for ``whisper jitsi`` and ``session new --jitsi``."""
    import tempfile

    ch = _normalize_cli_channel(channel, param_hint="channel")
    wb, mp = _resolve_whisper_paths_from_home(whisper_cpp_home, whisper_binary, model_path)

    if not wb:
        raise click.UsageError(
            f"Pass --whisper-binary or set {ENV_WHISPER_BINARY} "
            f"(defaults use ./whisper.cpp under $PWD or set {ENV_WHISPER_CPP_HOME})."
        )

    if not mp:
        raise click.UsageError(
            f"Pass --model / -m or set {ENV_WHISPER_MODEL} "
            f"(defaults use ./whisper.cpp under $PWD or set {ENV_WHISPER_CPP_HOME})."
        )

    tok = (publisher_token or "").strip()
    if not tok and not dry_run:
        raise click.UsageError(f"Pass --publisher-token or set {ENV_PUBLISHER_TOKEN}.")

    script = puller_script or default_jitsi_puller_script()

    try:
        extras = shlex.split(extra_whisper_args) if extra_whisper_args.strip() else []
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="extra-whisper-args") from e

    def _shell_for_pipe(pipe_path: str) -> str:
        return build_jitsi_pipeline_command(
            ffmpeg_bin=ffmpeg_bin,
            pipe_path=pipe_path,
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
        if mixed:
            fifo_path = tempfile.mktemp(suffix=".pcm", prefix="jitsi-audio-")  # noqa: S306
            click.echo(
                f"node {shlex.quote(script)} {shlex.quote(jitsi_url)} --mixed {shlex.quote(fifo_path)}"
            )
            click.echo(_shell_for_pipe(fifo_path))
        else:
            click.echo(
                f"node {shlex.quote(script)} {shlex.quote(jitsi_url)} --per-speaker --pipe-dir <tmpdir>"
            )
            click.echo(_shell_for_pipe("<track-fifo>"))
            click.echo(f"# max concurrent whisper instances: {max_speakers}")
        return

    async def _run() -> int:
        if mixed:
            fifo_path = tempfile.mktemp(suffix=".pcm", prefix="jitsi-audio-")  # noqa: S306
            return await run_jitsi_caption_pipeline(
                channel=ch,
                publisher_token=tok,
                jitsi_url=jitsi_url,
                node_bin=node_bin,
                puller_script=script,
                shell_command=_shell_for_pipe(fifo_path),
                fifo_path=fifo_path,
                line_kind=line_kind,
                debounce_ms=debounce_ms,
                min_interval_ms=min_interval_ms,
                quiet_ably_logs=True,
                verbose_echo_whisper=verbose,
            )

        return await run_jitsi_per_speaker_caption_pipeline(
            channel=ch,
            publisher_token=tok,
            jitsi_url=jitsi_url,
            node_bin=node_bin,
            puller_script=script,
            shell_command_for_pipe=_shell_for_pipe,
            line_kind=line_kind,
            debounce_ms=debounce_ms,
            min_interval_ms=min_interval_ms,
            quiet_ably_logs=True,
            max_speakers=max_speakers,
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
        "whisper.cpp checkout root; defaults to $PWD/whisper.cpp when unset. "
        "Fills in binary build/bin/whisper-stream-pcm and model "
        f"models/{WHISPER_CPP_DEFAULT_MODEL} under that root "
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
        "ggml model path or filename under models/ when using "
        "--whisper-cpp-home or $PWD/whisper.cpp defaults "
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


@whisper.command("jitsi")
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
@click.option(
    "--jitsi-url",
    type=str,
    required=True,
    help="Jitsi meeting URL (e.g. https://meet.jit.si/MyRoom).",
)
@click.option(
    "--node-bin",
    type=str,
    default="node",
    show_default=True,
    envvar=ENV_NODE_BIN,
    help=f"node executable (env {ENV_NODE_BIN}).",
)
@click.option(
    "--jitsi-puller-script",
    type=str,
    default=None,
    envvar=ENV_JITSI_PULLER_SCRIPT,
    help=(
        f"Path to jitsi-audio-puller index.js "
        f"(env {ENV_JITSI_PULLER_SCRIPT}, default: <project-root>/jitsi-audio-puller/index.js)."
    ),
)
@click.option("--ffmpeg", "ffmpeg_bin", default="ffmpeg", show_default=True, help="ffmpeg executable.")
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
        "whisper.cpp checkout root; defaults to $PWD/whisper.cpp when unset. "
        "Fills in binary build/bin/whisper-stream-pcm and model "
        f"models/{WHISPER_CPP_DEFAULT_MODEL} under that root "
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
        "ggml model path or filename under models/ when using "
        "--whisper-cpp-home or $PWD/whisper.cpp defaults "
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
    help="Print the node and ffmpeg | whisper commands and exit (no Ably, no subprocess).",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help=(
        "Per-speaker Jitsi mode: echo captioned lines (Name: text) to stdout. "
        "Otherwise echo whisper's raw stdout bytes to stderr."
    ),
)
@click.option(
    "--reconnect",
    "reconnect",
    is_flag=True,
    help=(
        "Reuse the cached publisher token for --channel instead of requiring --publisher-token. "
        "Errors out if no valid cached session exists for the channel."
    ),
)
@click.option(
    "--mixed",
    "mixed",
    is_flag=True,
    help="Sum all participants into one whisper stream (legacy mode). Default is per-speaker.",
)
@click.option(
    "--max-speakers",
    type=int,
    default=8,
    show_default=True,
    help="Max concurrent whisper instances in per-speaker mode.",
)
def whisper_jitsi(
    channel: str,
    publisher_token: str | None,
    jitsi_url: str,
    node_bin: str,
    jitsi_puller_script: str | None,
    ffmpeg_bin: str,
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
    reconnect: bool,
    mixed: bool,
    max_speakers: int,
) -> None:
    """Jitsi meeting → ffmpeg (WAV) → whisper-stream-pcm; publish stdout lines to Ably."""
    if reconnect:
        cached = session_cache.load_session(channel)
        if cached is None or not session_cache.is_valid(cached):
            raise click.ClickException(
                f"No valid cached session for channel {channel!r}. "
                "Run 'session new --jitsi ...' without --reconnect to mint and cache fresh tokens."
            )
        tok = cached["publisher_token"]
    else:
        tok = publisher_token or ""

    _run_whisper_jitsi(
        channel,
        tok,
        jitsi_url=jitsi_url,
        node_bin=node_bin,
        puller_script=jitsi_puller_script or default_jitsi_puller_script(),
        ffmpeg_bin=ffmpeg_bin,
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
        mixed=mixed,
        max_speakers=max_speakers,
    )


@main.group()
def signal() -> None:
    """Start caption sessions remotely via Signal (signal-cli)."""


@signal.command("link")
@click.option(
    "--signal-cli-bin",
    type=str,
    default=None,
    envvar=ENV_SIGNAL_CLI_BIN,
    help=f"signal-cli executable (env {ENV_SIGNAL_CLI_BIN}, default: signal-cli).",
)
@click.option(
    "--name",
    "device_name",
    type=str,
    default="captions",
    show_default=True,
    help="Linked-device name shown in your Signal app.",
)
def signal_link(signal_cli_bin: str | None, device_name: str) -> None:
    """Link this machine to your existing Signal account (one-time).

    Runs ``signal-cli link`` and prints an ``sgnl://linkdevice...`` URI. Encode it as a
    QR code (e.g. with ``qrencode -t ANSI``) and scan it from your phone via
    Signal -> Settings -> Linked Devices -> Link New Device.
    """
    import subprocess

    bin_path = (signal_cli_bin or "").strip() or default_signal_cli_bin()
    try:
        proc = subprocess.run([bin_path, "link", "-n", device_name], check=False)
    except FileNotFoundError as e:
        raise click.ClickException(
            f"Could not run {bin_path!r}: {e}. Install signal-cli or set {ENV_SIGNAL_CLI_BIN}."
        ) from e
    raise SystemExit(proc.returncode)


@signal.command("listen")
@click.option(
    "--account",
    type=str,
    default=None,
    envvar=ENV_SIGNAL_ACCOUNT,
    help=f"Signal account E.164 this device is linked to (env {ENV_SIGNAL_ACCOUNT}).",
)
@click.option(
    "--signal-cli-bin",
    type=str,
    default=None,
    envvar=ENV_SIGNAL_CLI_BIN,
    help=f"signal-cli executable (env {ENV_SIGNAL_CLI_BIN}, default: signal-cli).",
)
@click.option(
    "--allowed-senders",
    type=str,
    default=None,
    envvar=ENV_SIGNAL_ALLOWED_SENDERS,
    help=(
        "Comma-separated trusted sender E.164 numbers "
        f"(env {ENV_SIGNAL_ALLOWED_SENDERS}). Messages from anyone else are ignored."
    ),
)
@click.option(
    "--jitsi-hosts",
    type=str,
    default=None,
    envvar=ENV_SIGNAL_JITSI_HOSTS,
    help=(
        "Comma-separated allowed Jitsi hostnames "
        f"(env {ENV_SIGNAL_JITSI_HOSTS}, default: meet.jit.si)."
    ),
)
@click.option(
    "--any-jitsi-host",
    is_flag=True,
    envvar=ENV_SIGNAL_ANY_JITSI_HOST,
    help=(
        "Accept any well-formed https meeting URL (any domain), ignoring --jitsi-hosts. "
        f"Non-Jitsi links will simply fail to connect (env {ENV_SIGNAL_ANY_JITSI_HOST})."
    ),
)
@click.option(
    "--ttl",
    type=int,
    default=None,
    help=f"Token lifetime in seconds (default: 14400 or {ENV_TOKEN_TTL}).",
)
@click.option(
    "--captions-bin",
    type=str,
    default="captions",
    show_default=True,
    help="captions executable used to launch the Jitsi pipeline.",
)
@click.option(
    "--max-speakers",
    type=int,
    default=8,
    show_default=True,
    help="Max concurrent whisper instances for started sessions (per-speaker mode).",
)
@click.option(
    "--allow-self",
    is_flag=True,
    envvar=ENV_SIGNAL_ALLOW_SELF,
    help=(
        "Testing only: also honor commands you send from your own (linked) account, "
        f"disabling loop protection (env {ENV_SIGNAL_ALLOW_SELF})."
    ),
)
@click.option("-v", "--verbose", is_flag=True, help="Echo signal-cli stderr and captioned lines.")
def signal_listen(
    account: str | None,
    signal_cli_bin: str | None,
    allowed_senders: str | None,
    jitsi_hosts: str | None,
    any_jitsi_host: bool,
    ttl: int | None,
    captions_bin: str,
    max_speakers: int,
    allow_self: bool,
    verbose: bool,
) -> None:
    """Listen for trusted Signal messages and start/stop caption sessions on demand.

    A direct message containing an allowed Jitsi URL starts captions and replies with the
    subscriber link/token; ``stop`` ends the current session. signal-cli connects outbound
    only -- no router port and no remote shell access. Link this device first with
    ``captions signal link``.
    """
    acct = (account or "").strip()
    if not acct:
        raise click.UsageError(
            f"Pass --account or set {ENV_SIGNAL_ACCOUNT} to the Signal account E.164 "
            "this device is linked to."
        )

    bin_path = (signal_cli_bin or "").strip() or default_signal_cli_bin()

    if allowed_senders is not None:
        senders = {s.strip() for s in allowed_senders.split(",") if s.strip()}
    else:
        senders = signal_allowed_senders()

    if jitsi_hosts is not None:
        hosts = {h.strip().lower() for h in jitsi_hosts.split(",") if h.strip()}
    else:
        hosts = signal_jitsi_hosts()
    if not hosts:
        hosts = signal_jitsi_hosts()

    ttl_eff = ttl if ttl is not None else _default_ttl()

    from captions_relay.signal_listener import run_signal_listener

    try:
        exit_code = asyncio.run(
            run_signal_listener(
                account=acct,
                signal_cli_bin=bin_path,
                allowed_senders=senders,
                allowed_jitsi_hosts=hosts,
                ttl_seconds=ttl_eff,
                captions_bin=captions_bin,
                max_speakers=max_speakers,
                verbose=verbose,
                allow_self=allow_self,
                allow_any_jitsi_host=any_jitsi_host,
            )
        )
    except KeyboardInterrupt:
        click.echo("", err=True)
        raise SystemExit(130) from None
    raise SystemExit(exit_code)
