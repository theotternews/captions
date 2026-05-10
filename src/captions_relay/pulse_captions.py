"""Pulse → ffmpeg → whisper-stream-pcm → Ably caption publishes."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import logging
import os
import pty
import re
import shlex
import signal
import struct
import sys
import termios
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from ably.realtime.realtime import AblyRealtime
from ably.types.connectionstate import ConnectionState

from captions_relay.config import CAPTION_EVENT

log = logging.getLogger(__name__)

# Read pty master in chunks. Whisper uses `\r` + CSI when stdout is a TTY; with a bare pipe
# it falls back to newline logging (duplicated “growing” lines), so we attach a PTY to stdout.
_PULSE_WHISPER_READ_CHUNK = 16384


def _terminate_pulse_pipeline_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Terminate the whole ``ffmpeg | whisper`` shell pipeline.

    The subprocess is spawned with ``start_new_session=True``, so its PID is the
    process-group leader; ``killpg`` delivers SIGTERM to the shell and pipe
    children. ``proc.terminate()`` alone only signals the shell, which often
    leaves whisper/ffmpeg running after Ctrl-C (Python receives SIGINT, not the
    detached pipeline).
    """
    if proc is None or proc.returncode is not None or proc.pid is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()


def _pulse_pty_set_winsize(slave_fd: int) -> None:
    """Size the slave PTY like our stderr terminal so whisper’s layout matches an interactive run."""
    try:
        if sys.stderr.isatty():
            dim = os.get_terminal_size(sys.stderr.fileno())
            rows, cols = dim.lines, dim.columns
        else:
            rows, cols = 24, 80
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


# whisper.cpp streaming uses CSI sequences (e.g. ESC [ 2 K clear line) and CR rewrites.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def normalize_whisper_stdout_line(raw: str, *, min_dup_prefix: int = 4) -> str:
    """Turn TTY-oriented whisper stdout into plain caption text.

    Subscriber pages mirror this helper. With ``--line-kind auto``, the pulse pipeline
    also uses it when slicing PTY stdout into incremental ``partial`` / ``final`` publishes.
    """
    s = _ANSI_OSC_RE.sub("", raw)
    s = _ANSI_CSI_RE.sub("", s)
    if "\r" in s:
        s = s.split("\r")[-1]
    s = _collapse_redundant_prefix_repeat(s.strip(), min_prefix=min_dup_prefix)
    return s.strip()


def _collapse_redundant_prefix_repeat(s: str, *, min_prefix: int) -> str:
    """If ``s == prefix + prefix + tail`` where ``tail`` continues an amended line, keep ``prefix + tail``."""
    while len(s) >= min_prefix * 2:
        cut = None
        for i in range(min_prefix, len(s)):
            prefix = s[:i]
            rest = s[i:]
            if rest.startswith(prefix):
                cut = i
                break
        if cut is None:
            break
        s = s[cut:]
    return s


class WhisperStdoutStreamProcessor:
    """Incremental UTF-8 decode of whisper PTY stdout → caption (text, kind) events.

    * **auto** — ``\\r`` amendments publish as ``partial`` (deduped snapshot); ``\\n`` publishes
      ``final``. Matches `normalize_whisper_stdout_line` semantics (mirrors subscriber).
    * **final** / **partial** — only ``\\n``-delimited logical lines are emitted, all with that kind
      (legacy pulse behavior without interim ``\\r`` traffic).
    """

    def __init__(self, *, line_kind: str) -> None:
        if line_kind not in {"auto", "final", "partial"}:
            raise ValueError(f"line_kind must be auto, final, or partial, not {line_kind!r}")
        self._line_kind = line_kind
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line_tail = ""
        self._last_partial_norm = ""

    def feed(self, data: bytes) -> list[tuple[str, str]]:
        self._line_tail += self._decoder.decode(data, final=False)
        if self._line_kind == "auto":
            return self._feed_auto()
        return self._feed_forced()

    def _emit_auto_segment(self, segment: str, events: list[tuple[str, str]]) -> None:
        idx = 0
        while True:
            j = segment.find("\r", idx)
            if j < 0:
                break
            snap = normalize_whisper_stdout_line(segment[: j + 1])
            if snap and snap != self._last_partial_norm:
                events.append((snap, "partial"))
                self._last_partial_norm = snap
            idx = j + 1
        snap = normalize_whisper_stdout_line(segment)
        if "\r" in segment and snap and snap != self._last_partial_norm:
            events.append((snap, "partial"))
            self._last_partial_norm = snap

    def _feed_auto(self) -> list[tuple[str, str]]:
        """Each ``\\n`` in the UTF-8 stream ends a logical line → one ``final`` (plus any ``\\r`` partials inside that line)."""
        events: list[tuple[str, str]] = []
        while "\n" in self._line_tail:
            nl = self._line_tail.index("\n")
            segment = self._line_tail[:nl]
            self._line_tail = self._line_tail[nl + 1 :]
            # CRLF: drop the ``\\r`` that immediately precedes ``\\n`` so ``normalize`` does
            # not treat it as a TTY rewrite and return "".
            if segment.endswith("\r"):
                segment = segment[:-1]
            self._emit_auto_segment(segment, events)
            self._last_partial_norm = ""
            fin = normalize_whisper_stdout_line(segment)
            if fin:
                events.append((fin, "final"))
        snap = normalize_whisper_stdout_line(self._line_tail)
        if snap and snap != self._last_partial_norm:
            events.append((snap, "partial"))
            self._last_partial_norm = snap
        return events

    def _feed_forced(self) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        kind = self._line_kind
        while "\n" in self._line_tail:
            line, _, rest = self._line_tail.partition("\n")
            self._line_tail = rest
            if line.endswith("\r"):
                line = line[:-1]
            t = normalize_whisper_stdout_line(line)
            if t:
                events.append((t, kind))
        return events

    def close(self) -> list[tuple[str, str]]:
        self._line_tail += self._decoder.decode(b"", final=True)
        if self._line_kind == "auto":
            events: list[tuple[str, str]] = []
            while "\n" in self._line_tail:
                nl = self._line_tail.index("\n")
                segment = self._line_tail[:nl]
                self._line_tail = self._line_tail[nl + 1 :]
                if segment.endswith("\r"):
                    segment = segment[:-1]
                self._emit_auto_segment(segment, events)
                self._last_partial_norm = ""
                fin = normalize_whisper_stdout_line(segment)
                if fin:
                    events.append((fin, "final"))
            snap = normalize_whisper_stdout_line(self._line_tail)
            if snap:
                # Incomplete logical line when the subprocess exits — commit once as final so
                # subscribers can move scrollback past #current-only partials (CaptionThrottle
                # will skip the redundant partial send if merged with the following final).
                events.append((snap, "final"))
            self._line_tail = ""
            return events
        events = []
        kind = self._line_kind
        while "\n" in self._line_tail:
            line, _, rest = self._line_tail.partition("\n")
            self._line_tail = rest
            if line.endswith("\r"):
                line = line[:-1]
            t = normalize_whisper_stdout_line(line)
            if t:
                events.append((t, kind))
        t = normalize_whisper_stdout_line(self._line_tail)
        if t:
            events.append((t, kind))
        self._line_tail = ""
        return events


class CaptionThrottle:
    """Match `web/publisher/glue.js` debounce + min-interval for partials."""

    def __init__(
        self,
        publish: Callable[[dict], Awaitable[None]],
        *,
        debounce_s: float,
        min_interval_s: float,
    ) -> None:
        self._publish = publish
        self._debounce_s = debounce_s
        self._min_interval_s = min_interval_s
        self._pending: dict | None = None
        self._debounce_task: asyncio.Task[None] | None = None
        self._spacing_task: asyncio.Task[None] | None = None
        self._last_flush = 0.0
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True
        await self._cancel_task(self._debounce_task)
        self._debounce_task = None
        await self._cancel_task(self._spacing_task)
        self._spacing_task = None

    async def _cancel_task(self, t: asyncio.Task[None] | None) -> None:
        if t is None or t.done():
            return
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    async def push(self, text: str, kind: str) -> None:
        if self._closed:
            return
        body = {
            "t": datetime.now(timezone.utc).isoformat(),
            "text": text.strip(),
            "kind": "final" if kind == "final" else "partial",
        }
        if not body["text"]:
            return

        if body["kind"] == "final":
            await self._cancel_task(self._debounce_task)
            self._debounce_task = None
            await self._cancel_task(self._spacing_task)
            self._spacing_task = None
            self._pending = None
            await self._send_with_spacing(body)
            return

        self._pending = body
        await self._cancel_task(self._debounce_task)

        async def debounced() -> None:
            try:
                await asyncio.sleep(self._debounce_s)
                latest = self._pending
                self._pending = None
                self._debounce_task = None
                if latest and latest["kind"] == "partial":
                    await self._send_with_spacing(latest)
            except asyncio.CancelledError:
                pass

        self._debounce_task = asyncio.create_task(debounced())

    async def _send_with_spacing(self, body: dict) -> None:
        if not body["text"]:
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        elapsed = now - self._last_flush
        delay = max(0.0, self._min_interval_s - elapsed)

        async def send() -> None:
            self._last_flush = asyncio.get_running_loop().time()
            try:
                await self._publish(body)
            except Exception as e:
                log.warning("Ably publish failed: %s", e)

        if delay > 0 and body["kind"] != "final":
            await self._cancel_task(self._spacing_task)

            async def spaced() -> None:
                try:
                    await asyncio.sleep(delay)
                    await send()
                except asyncio.CancelledError:
                    pass

            self._spacing_task = asyncio.create_task(spaced())
        else:
            await send()


def build_pulse_pipeline_command(
    *,
    ffmpeg_bin: str,
    pulse_device: str,
    sample_rate: int,
    whisper_bin: str,
    model_path: str,
    pcm_format: str,
    step_ms: int,
    length_ms: int,
    keep_ms: int,
    extra_whisper_args: list[str],
) -> str:
    ffmpeg_argv = [
        ffmpeg_bin,
        "-loglevel",
        "quiet",
        "-f",
        "pulse",
        "-i",
        pulse_device,
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-f",
        "wav",
        "-",
    ]
    whisper_argv = [
        whisper_bin,
        "-m",
        model_path,
        "--format",
        pcm_format,
        "--sample-rate",
        str(sample_rate),
        "--step",
        str(step_ms),
        "--length",
        str(length_ms),
        "--keep",
        str(keep_ms),
        *extra_whisper_args,
    ]
    return f"{shlex.join(ffmpeg_argv)} | {shlex.join(whisper_argv)}"


async def _pulse_push_caption_events(throttle: CaptionThrottle, events: list[tuple[str, str]]) -> None:
    for text, kind in events:
        await throttle.push(text, kind)


async def wait_ably_connected(realtime: AblyRealtime, *, timeout_s: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        state = realtime.connection.state
        if state == ConnectionState.CONNECTED:
            return
        if state == ConnectionState.FAILED:
            reason = realtime.connection.error_reason
            msg = str(reason) if reason else "unknown error"
            raise RuntimeError(f"Ably connection failed: {msg}")
        await asyncio.sleep(0.05)
    raise TimeoutError("timed out waiting for Ably realtime connection")


async def run_pulse_caption_pipeline(
    *,
    channel: str,
    publisher_token: str,
    shell_command: str,
    line_kind: str,
    debounce_ms: int,
    min_interval_ms: int,
    quiet_ably_logs: bool,
    verbose_echo_whisper: bool = False,
) -> int:
    if sys.platform == "win32":
        raise RuntimeError("pulse capture is Unix-only; use WSL or a different audio source.")

    if quiet_ably_logs:
        logging.getLogger("ably").setLevel(logging.WARNING)

    realtime = AblyRealtime(token=publisher_token.strip())
    ch = realtime.channels.get(channel)
    throttle = CaptionThrottle(
        lambda body: ch.publish(CAPTION_EVENT, body),
        debounce_s=debounce_ms / 1000.0,
        min_interval_s=min_interval_ms / 1000.0,
    )

    proc: asyncio.subprocess.Process | None = None
    exit_code = 0
    transport: asyncio.BaseTransport | None = None
    master_fd: int | None = None
    slave_fd: int | None = None
    loop: asyncio.AbstractEventLoop | None = None
    stopped = asyncio.Event()
    try:
        await wait_ably_connected(realtime)

        loop = asyncio.get_running_loop()
        master_fd, slave_fd = pty.openpty()
        _pulse_pty_set_winsize(slave_fd)
        proc = await asyncio.create_subprocess_shell(
            shell_command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=None,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = None

        def _on_shutdown_signal() -> None:
            _terminate_pulse_pipeline_proc(proc)
            stopped.set()

        loop.add_signal_handler(signal.SIGINT, _on_shutdown_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_shutdown_signal)
        try:
            reader = asyncio.StreamReader()
            pipe_f = os.fdopen(master_fd, "rb", buffering=0, closefd=True)
            master_fd = None
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader),
                pipe_f,
            )

            if not verbose_echo_whisper:
                log.info("Started ffmpeg | whisper pipeline (pid %s, whisper stdout is a PTY)", proc.pid)

            proc_wait = asyncio.create_task(proc.wait())
            whisper_out = WhisperStdoutStreamProcessor(line_kind=line_kind)
            try:
                breaking = False
                user_stopped = False
                while not breaking:
                    read_task = asyncio.create_task(reader.read(_PULSE_WHISPER_READ_CHUNK))
                    stop_task = asyncio.create_task(stopped.wait())
                    done_tasks, _ = await asyncio.wait(
                        {read_task, stop_task, proc_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if stop_task in done_tasks:
                        read_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await read_task
                        if not proc_wait.done():
                            proc_wait.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await proc_wait
                        user_stopped = True
                        breaking = True
                        continue

                    if proc_wait in done_tasks:
                        read_task.cancel()
                        stop_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await read_task
                        with contextlib.suppress(asyncio.CancelledError):
                            await stop_task
                        breaking = True
                        continue

                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task

                    chunk = read_task.result()

                    if not chunk:
                        breaking = True
                        continue

                    if verbose_echo_whisper:
                        sys.stderr.buffer.write(chunk)
                        sys.stderr.buffer.flush()
                    await _pulse_push_caption_events(throttle, whisper_out.feed(chunk))

                if not user_stopped:
                    while True:
                        chunk = await reader.read(_PULSE_WHISPER_READ_CHUNK)
                        if not chunk:
                            break
                        if verbose_echo_whisper:
                            sys.stderr.buffer.write(chunk)
                            sys.stderr.buffer.flush()
                        await _pulse_push_caption_events(throttle, whisper_out.feed(chunk))

                # Always flush processor state (EOF final for auto tail; forced modes). Omitting
                # this on Ctrl+C/user stop left scrollback subscriber history empty (#current only).
                await _pulse_push_caption_events(throttle, whisper_out.close())
            finally:
                if not proc_wait.done():
                    proc_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await proc_wait
        finally:
            if loop is not None:
                with contextlib.suppress(ValueError, NotImplementedError):
                    loop.remove_signal_handler(signal.SIGINT)
                with contextlib.suppress(ValueError, NotImplementedError):
                    loop.remove_signal_handler(signal.SIGTERM)
    finally:
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if transport is not None:
            transport.close()
        await throttle.aclose()
        if proc is not None and proc.returncode is None:
            _terminate_pulse_pipeline_proc(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=8.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    if proc.pid is not None:
                        os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        await realtime.close()

    if proc is not None:
        exit_code = proc.returncode if proc.returncode is not None else 0
        if exit_code != 0 and not verbose_echo_whisper:
            log.info("ffmpeg | whisper exited with code %s", exit_code)

    return exit_code
