"""Pulse → ffmpeg → whisper-stream-pcm → Ably caption publishes."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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

# Bracketed status markers whisper.cpp streaming prints around the transcript itself
# (not speech); dropped so they never reach subscribers.
_WHISPER_CONTROL_LINE_RE = re.compile(r"^\[\s*Start (?:streaming|speaking)\s*\]$", re.IGNORECASE)


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
    s = s.strip()
    if _WHISPER_CONTROL_LINE_RE.match(s):
        return ""
    return s


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


def build_jitsi_pipeline_command(
    *,
    ffmpeg_bin: str,
    pipe_path: str,
    sample_rate: int,
    whisper_bin: str,
    model_path: str,
    pcm_format: str,
    step_ms: int,
    length_ms: int,
    keep_ms: int,
    extra_whisper_args: list[str],
) -> str:
    """Build the ``ffmpeg | whisper-stream-pcm`` shell command for a Jitsi FIFO source.

    Reads raw PCM s16le at 48 kHz from ``pipe_path`` and resamples to ``sample_rate``
    (typically 16 kHz) before piping into whisper-stream-pcm.
    """
    ffmpeg_argv = [
        ffmpeg_bin,
        "-loglevel",
        "quiet",
        "-f",
        "s16le",
        "-i",
        pipe_path,
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


async def _pulse_push_caption_events(
    throttle: CaptionThrottle,
    events: list[tuple[str, str]],
    *,
    verbose_speaker: SpeakerInfo | None = None,
) -> None:
    for text, kind in events:
        if verbose_speaker is not None:
            echo_verbose_caption(text, kind, speaker=verbose_speaker)
        await throttle.push(text, kind)


@dataclass(frozen=True)
class SpeakerInfo:
    participant_id: str
    name: str


def format_speaker_line(name: str | None, text: str) -> str:
    """Format a caption line with an optional speaker prefix (mirrors subscriber UI)."""
    label = (name or "").strip()
    body = text.strip()
    if not label:
        return body
    if not body:
        return f"{label}:"
    return f"{label}: {body}"


_verbose_stdout_lock = threading.Lock()


def echo_verbose_caption(text: str, kind: str, *, speaker: SpeakerInfo | None) -> None:
    """Mirror subscriber caption lines on stdout when ``-v`` is used (per-speaker Jitsi mode)."""
    line = format_speaker_line(speaker.name if speaker else None, text)
    if not line:
        return
    with _verbose_stdout_lock:
        if kind == "final":
            sys.stdout.write(line + "\n")
        else:
            sys.stdout.write("\r" + line)
        sys.stdout.flush()


def build_caption_body(
    text: str,
    kind: str,
    *,
    speaker: SpeakerInfo | None = None,
) -> dict:
    body = {
        "t": datetime.now(timezone.utc).isoformat(),
        "text": text.strip(),
        "kind": "final" if kind == "final" else "partial",
    }
    if speaker is not None:
        body["speaker"] = {"id": speaker.participant_id, "name": speaker.name}
    return body


def _speaker_publish(
    ch,
    speaker: SpeakerInfo,
) -> Callable[[dict], Awaitable[None]]:
    async def publish(body: dict) -> None:
        text = str(body.get("text", "")).strip()
        payload = {
            **body,
            "text": format_speaker_line(speaker.name, text),
            "speaker": {"id": speaker.participant_id, "name": speaker.name},
        }
        await ch.publish(CAPTION_EVENT, payload)

    return publish


async def _run_whisper_shell_loop(
    *,
    shell_command: str,
    throttle: CaptionThrottle,
    line_kind: str,
    verbose_echo_whisper: bool,
    verbose_speaker: SpeakerInfo | None = None,
    stopped: asyncio.Event | None = None,
) -> int:
    """Run one ``ffmpeg | whisper`` shell pipeline until it exits or ``stopped`` is set."""
    echo_raw_whisper = verbose_echo_whisper and verbose_speaker is None
    echo_caption_speaker = verbose_speaker if verbose_echo_whisper else None
    proc: asyncio.subprocess.Process | None = None
    exit_code = 0
    transport: asyncio.BaseTransport | None = None
    master_fd: int | None = None
    slave_fd: int | None = None
    loop = asyncio.get_running_loop()
    stop_event = stopped or asyncio.Event()

    try:
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

        reader = asyncio.StreamReader()
        pipe_f = os.fdopen(master_fd, "rb", buffering=0, closefd=True)
        master_fd = None
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            pipe_f,
        )

        proc_wait = asyncio.create_task(proc.wait())
        whisper_out = WhisperStdoutStreamProcessor(line_kind=line_kind)
        try:
            breaking = False
            while not breaking:
                read_task = asyncio.create_task(reader.read(_PULSE_WHISPER_READ_CHUNK))
                stop_task = asyncio.create_task(stop_event.wait())
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
                    breaking = True
                    continue

                if proc_wait in done_tasks:
                    read_task.cancel()
                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, OSError):
                        await read_task
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                    breaking = True
                    continue

                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task

                try:
                    chunk = read_task.result()
                except OSError:
                    breaking = True
                    continue

                if not chunk:
                    breaking = True
                    continue

                if echo_raw_whisper:
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.buffer.flush()
                await _pulse_push_caption_events(
                    throttle,
                    whisper_out.feed(chunk),
                    verbose_speaker=echo_caption_speaker,
                )

            if not stop_event.is_set():
                while True:
                    try:
                        chunk = await reader.read(_PULSE_WHISPER_READ_CHUNK)
                    except OSError:
                        break
                    if not chunk:
                        break
                    if echo_raw_whisper:
                        sys.stderr.buffer.write(chunk)
                        sys.stderr.buffer.flush()
                    await _pulse_push_caption_events(
                        throttle,
                        whisper_out.feed(chunk),
                        verbose_speaker=echo_caption_speaker,
                    )

            await _pulse_push_caption_events(
                throttle,
                whisper_out.close(),
                verbose_speaker=echo_caption_speaker,
            )
        finally:
            if not proc_wait.done():
                proc_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await proc_wait
    finally:
        if slave_fd is not None:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if transport is not None:
            transport.close()
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

    if proc is not None:
        exit_code = proc.returncode if proc.returncode is not None else 0
    return exit_code


class SpeakerPipelineTask:
    """One remote audio track → one whisper subprocess → Ably publishes with speaker metadata."""

    def __init__(
        self,
        *,
        track_id: str,
        speaker: SpeakerInfo,
        pipe_path: str,
        shell_command: str,
        publish: Callable[[dict], Awaitable[None]],
        line_kind: str,
        debounce_ms: int,
        min_interval_ms: int,
        verbose_echo_whisper: bool,
        on_exit: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.track_id = track_id
        self.speaker = speaker
        self.pipe_path = pipe_path
        self.shell_command = shell_command
        self._publish = publish
        self._line_kind = line_kind
        self._debounce_ms = debounce_ms
        self._min_interval_ms = min_interval_ms
        self._verbose_echo_whisper = verbose_echo_whisper
        self._on_exit = on_exit
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[int] | None = None
        self._throttle: CaptionThrottle | None = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running():
            return
        if self._task is not None:
            self._task = None
            if self._throttle is not None:
                await self._throttle.aclose()
                self._throttle = None
        self._stopped.clear()
        self._throttle = CaptionThrottle(
            self._publish,
            debounce_s=self._debounce_ms / 1000.0,
            min_interval_s=self._min_interval_ms / 1000.0,
        )
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> int:
        assert self._throttle is not None
        exit_cb = self._on_exit
        try:
            return await _run_whisper_shell_loop(
                shell_command=self.shell_command,
                throttle=self._throttle,
                line_kind=self._line_kind,
                verbose_echo_whisper=self._verbose_echo_whisper,
                verbose_speaker=self.speaker,
                stopped=self._stopped,
            )
        finally:
            await self._throttle.aclose()
            intentional = self._stopped.is_set()
            self._task = None
            self._throttle = None
            if exit_cb is not None and not intentional:
                with contextlib.suppress(Exception):
                    await exit_cb(self.track_id)

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        if self._throttle is not None:
            await self._throttle.aclose()
            self._throttle = None


class SpeakerOrchestrator:
    """Manage per-track whisper pipelines driven by jitsi-audio-puller stdout control events."""

    def __init__(
        self,
        *,
        ch,
        shell_command_for_pipe: Callable[[str], str],
        line_kind: str,
        debounce_ms: int,
        min_interval_ms: int,
        max_speakers: int,
        verbose_echo_whisper: bool,
    ) -> None:
        self._ch = ch
        self._shell_command_for_pipe = shell_command_for_pipe
        self._line_kind = line_kind
        self._debounce_ms = debounce_ms
        self._min_interval_ms = min_interval_ms
        self._max_speakers = max_speakers
        self._verbose_echo_whisper = verbose_echo_whisper
        self._pipelines: dict[str, SpeakerPipelineTask] = {}
        self._track_speakers: dict[str, SpeakerInfo] = {}
        self._muted_tracks: set[str] = set()
        self._waiting_for_slot: set[str] = set()

    def _running_count(self) -> int:
        return sum(1 for p in self._pipelines.values() if p.is_running())

    async def _on_pipeline_exit(self, track_id: str) -> None:
        if track_id not in self._pipelines or track_id in self._muted_tracks:
            return
        speaker = self._track_speakers.get(track_id)
        label = speaker.name if speaker else track_id
        log.warning("Whisper exited unexpectedly for %s — restarting in 1s", label)
        await asyncio.sleep(1.0)
        if track_id in self._pipelines and track_id not in self._muted_tracks:
            await self._try_start_pipeline(track_id)

    async def _try_start_pipeline(self, track_id: str) -> None:
        pipeline = self._pipelines.get(track_id)
        if pipeline is None or track_id in self._muted_tracks:
            return
        if pipeline.is_running():
            return
        if self._running_count() >= self._max_speakers:
            if track_id not in self._waiting_for_slot:
                speaker = self._track_speakers.get(track_id)
                label = speaker.name if speaker else track_id
                self._waiting_for_slot.add(track_id)
                log.warning(
                    "Max speakers (%s) reached — %s waiting for a transcription slot",
                    self._max_speakers,
                    label,
                )
            return

        self._waiting_for_slot.discard(track_id)
        await pipeline.start()
        speaker = self._track_speakers.get(track_id)
        label = speaker.name if speaker else track_id
        log.info("Started per-speaker whisper for %s (%s)", label, track_id)

    async def _promote_waiting(self) -> None:
        for track_id in list(self._waiting_for_slot):
            if self._running_count() >= self._max_speakers:
                break
            await self._try_start_pipeline(track_id)

    async def aclose(self) -> None:
        await self._stop_all()

    async def _stop_all(self) -> None:
        tasks = [p.stop() for p in self._pipelines.values()]
        self._pipelines.clear()
        self._track_speakers.clear()
        self._muted_tracks.clear()
        self._waiting_for_slot.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_control_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.debug("Ignoring non-JSON puller stdout: %s", line[:120])
            return

        event = msg.get("event")
        if event == "ready":
            log.info("Jitsi puller ready — waiting for remote audio tracks")
            return

        if event != "track":
            return

        action = msg.get("action")
        track_id = msg.get("trackId")
        if not track_id:
            return

        if action == "added":
            await self._on_track_added(msg)
        elif action == "removed":
            await self._on_track_removed(track_id)
        elif action == "mute":
            await self._on_track_mute(track_id, bool(msg.get("muted")))

    async def _on_track_added(self, msg: dict) -> None:
        track_id = str(msg["trackId"])
        participant_id = str(msg.get("participantId") or track_id)
        name = str(msg.get("name") or participant_id)
        pipe_path = str(msg.get("pipe") or "")
        if not pipe_path:
            log.warning("Track added without pipe path: %s", track_id)
            return

        if track_id in self._pipelines:
            return

        if track_id in self._pipelines:
            return

        speaker = SpeakerInfo(participant_id=participant_id, name=name)
        self._track_speakers[track_id] = speaker
        pipeline = SpeakerPipelineTask(
            track_id=track_id,
            speaker=speaker,
            pipe_path=pipe_path,
            shell_command=self._shell_command_for_pipe(pipe_path),
            publish=_speaker_publish(self._ch, speaker),
            line_kind=self._line_kind,
            debounce_ms=self._debounce_ms,
            min_interval_ms=self._min_interval_ms,
            verbose_echo_whisper=self._verbose_echo_whisper,
            on_exit=self._on_pipeline_exit,
        )
        self._pipelines[track_id] = pipeline
        if msg.get("muted"):
            self._muted_tracks.add(track_id)
            log.info("Track added (muted): %s (%s)", name, track_id)
            return

        await self._try_start_pipeline(track_id)

    async def _on_track_removed(self, track_id: str) -> None:
        pipeline = self._pipelines.pop(track_id, None)
        self._track_speakers.pop(track_id, None)
        self._muted_tracks.discard(track_id)
        self._waiting_for_slot.discard(track_id)
        if pipeline is not None:
            await pipeline.stop()
            log.info("Stopped per-speaker whisper for track %s", track_id)
        await self._promote_waiting()

    async def _on_track_mute(self, track_id: str, muted: bool) -> None:
        pipeline = self._pipelines.get(track_id)
        if pipeline is None:
            log.warning(
                "Mute event for unknown track %s (muted=%s) — no whisper pipeline",
                track_id,
                muted,
            )
            return
        if muted:
            self._muted_tracks.add(track_id)
            self._waiting_for_slot.discard(track_id)
            await pipeline.stop()
            log.info("Paused whisper for muted track %s", track_id)
            await self._promote_waiting()
            return

        self._muted_tracks.discard(track_id)
        await self._try_start_pipeline(track_id)
        log.info("Resumed whisper for unmuted track %s", track_id)


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
                        with contextlib.suppress(asyncio.CancelledError, OSError):
                            await read_task
                        with contextlib.suppress(asyncio.CancelledError):
                            await stop_task
                        breaking = True
                        continue

                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task

                    try:
                        chunk = read_task.result()
                    except OSError:
                        # PTY hangup (EIO) when the subprocess exits abruptly; treat as EOF.
                        breaking = True
                        continue

                    if not chunk:
                        breaking = True
                        continue

                    if verbose_echo_whisper:
                        sys.stderr.buffer.write(chunk)
                        sys.stderr.buffer.flush()
                    await _pulse_push_caption_events(throttle, whisper_out.feed(chunk))

                if not user_stopped:
                    while True:
                        try:
                            chunk = await reader.read(_PULSE_WHISPER_READ_CHUNK)
                        except OSError:
                            # PTY hangup (EIO) after subprocess exit; treat as EOF.
                            break
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


# Node exit code that signals a transient Jitsi failure (shard change, connection
# drop, conference failed) — Python should recreate the FIFO and retry.
_NODE_RETRY_EXIT = 1
_RETRY_BASE_DELAY = 2.0
_RETRY_MAX_DELAY = 30.0
# If an attempt stayed alive this long it probably connected successfully, so
# reset the backoff delay rather than continuing to increase it.
_RETRY_LONG_RUN_THRESHOLD = 10.0


async def run_jitsi_caption_pipeline(
    *,
    channel: str,
    publisher_token: str,
    jitsi_url: str,
    node_bin: str,
    puller_script: str,
    shell_command: str,
    fifo_path: str,
    line_kind: str,
    debounce_ms: int,
    min_interval_ms: int,
    quiet_ably_logs: bool,
    verbose_echo_whisper: bool = False,
) -> int:
    """Start ``node jitsi-audio-puller`` then run ``run_pulse_caption_pipeline``.

    The FIFO at ``fifo_path`` is created (and recreated on each retry) by this
    function.  The caller must NOT pre-create it.  The FIFO is removed in the
    outer ``finally`` block once all retry attempts are exhausted.

    When the Node process exits with :data:`_NODE_RETRY_EXIT` (transient Jitsi
    failure: shard change, connection drop, conference failed) the pipeline is
    restarted automatically with exponential backoff up to
    :data:`_RETRY_MAX_DELAY` seconds.  Any other exit code ends the loop.
    """
    delay = _RETRY_BASE_DELAY
    attempt = 0
    exit_code = 0

    try:
        while True:
            # Recreate the FIFO fresh for each attempt (same path, new inode).
            with contextlib.suppress(FileNotFoundError, OSError):
                os.unlink(fifo_path)
            os.mkfifo(fifo_path)

            node_proc: asyncio.subprocess.Process | None = None
            node_exit: int | None = None
            attempt_start = asyncio.get_event_loop().time()
            try:
                node_proc = await asyncio.create_subprocess_exec(
                    node_bin,
                    puller_script,
                    jitsi_url,
                    "--mixed",
                    fifo_path,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=None,
                    stderr=None,
                )
                if attempt == 0:
                    log.info("Started jitsi-audio-puller (pid %s) for %s", node_proc.pid, jitsi_url)
                else:
                    log.info(
                        "Restarted jitsi-audio-puller (pid %s) for %s (attempt %s)",
                        node_proc.pid, jitsi_url, attempt + 1,
                    )

                exit_code = await run_pulse_caption_pipeline(
                    channel=channel,
                    publisher_token=publisher_token,
                    shell_command=shell_command,
                    line_kind=line_kind,
                    debounce_ms=debounce_ms,
                    min_interval_ms=min_interval_ms,
                    quiet_ably_logs=quiet_ably_logs,
                    verbose_echo_whisper=verbose_echo_whisper,
                )
            finally:
                if node_proc is not None:
                    if node_proc.returncode is None:
                        node_proc.terminate()
                        try:
                            await asyncio.wait_for(node_proc.wait(), timeout=5.0)
                        except TimeoutError:
                            with contextlib.suppress(ProcessLookupError, PermissionError):
                                node_proc.kill()
                            await node_proc.wait()
                    node_exit = node_proc.returncode

            elapsed = asyncio.get_event_loop().time() - attempt_start
            if node_exit == _NODE_RETRY_EXIT:
                if elapsed >= _RETRY_LONG_RUN_THRESHOLD:
                    delay = _RETRY_BASE_DELAY
                log.info(
                    "Jitsi connection lost (transient), reconnecting in %.0fs…",
                    delay,
                )
                await asyncio.sleep(delay)
                if elapsed < _RETRY_LONG_RUN_THRESHOLD:
                    delay = min(delay * 2, _RETRY_MAX_DELAY)
                attempt += 1
                continue

            break
    finally:
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(fifo_path)

    return exit_code


async def run_jitsi_per_speaker_caption_pipeline(
    *,
    channel: str,
    publisher_token: str,
    jitsi_url: str,
    node_bin: str,
    puller_script: str,
    shell_command_for_pipe: Callable[[str], str],
    line_kind: str,
    debounce_ms: int,
    min_interval_ms: int,
    quiet_ably_logs: bool,
    max_speakers: int = 8,
    verbose_echo_whisper: bool = False,
) -> int:
    """Start per-track jitsi-audio-puller and spawn one whisper pipeline per remote speaker."""
    if sys.platform == "win32":
        raise RuntimeError("jitsi capture is Unix-only; use WSL or a different audio source.")

    if quiet_ably_logs:
        logging.getLogger("ably").setLevel(logging.WARNING)

    delay = _RETRY_BASE_DELAY
    attempt = 0
    exit_code = 0

    while True:
        pipe_dir = tempfile.mkdtemp(prefix="jitsi-speakers-")
        node_proc: asyncio.subprocess.Process | None = None
        node_exit: int | None = None
        attempt_start = asyncio.get_event_loop().time()
        realtime: AblyRealtime | None = None
        orchestrator: SpeakerOrchestrator | None = None
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_shutdown_signal() -> None:
            stopped.set()
            if node_proc is not None and node_proc.returncode is None:
                node_proc.terminate()

        loop.add_signal_handler(signal.SIGINT, _on_shutdown_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_shutdown_signal)

        try:
            node_proc = await asyncio.create_subprocess_exec(
                node_bin,
                puller_script,
                jitsi_url,
                "--per-speaker",
                "--pipe-dir",
                pipe_dir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
            )
            if attempt == 0:
                log.info(
                    "Started jitsi-audio-puller (pid %s, per-speaker) for %s",
                    node_proc.pid,
                    jitsi_url,
                )
            else:
                log.info(
                    "Restarted jitsi-audio-puller (pid %s, per-speaker) for %s (attempt %s)",
                    node_proc.pid,
                    jitsi_url,
                    attempt + 1,
                )

            realtime = AblyRealtime(token=publisher_token.strip())
            await wait_ably_connected(realtime)
            ch = realtime.channels.get(channel)
            orchestrator = SpeakerOrchestrator(
                ch=ch,
                shell_command_for_pipe=shell_command_for_pipe,
                line_kind=line_kind,
                debounce_ms=debounce_ms,
                min_interval_ms=min_interval_ms,
                max_speakers=max_speakers,
                verbose_echo_whisper=verbose_echo_whisper,
            )

            assert node_proc.stdout is not None
            stdout_reader = asyncio.create_task(_read_puller_stdout(node_proc.stdout, orchestrator))
            node_wait = asyncio.create_task(node_proc.wait())
            stop_wait = asyncio.create_task(stopped.wait())

            done, _ = await asyncio.wait(
                {stdout_reader, node_wait, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_wait in done:
                exit_code = 130
            elif node_wait in done:
                exit_code = node_wait.result() or 0
            else:
                exit_code = 0

            stdout_reader.cancel()
            node_wait.cancel()
            stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stdout_reader
            with contextlib.suppress(asyncio.CancelledError):
                await node_wait
            with contextlib.suppress(asyncio.CancelledError):
                await stop_wait

        finally:
            with contextlib.suppress(ValueError, NotImplementedError):
                loop.remove_signal_handler(signal.SIGINT)
            with contextlib.suppress(ValueError, NotImplementedError):
                loop.remove_signal_handler(signal.SIGTERM)

            if orchestrator is not None:
                await orchestrator.aclose()
            if node_proc is not None and node_proc.returncode is None:
                node_proc.terminate()
                try:
                    await asyncio.wait_for(node_proc.wait(), timeout=5.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        node_proc.kill()
                    await node_proc.wait()
            if node_proc is not None:
                node_exit = node_proc.returncode
            if realtime is not None:
                await realtime.close()
            shutil.rmtree(pipe_dir, ignore_errors=True)

        if stopped.is_set():
            break

        elapsed = asyncio.get_event_loop().time() - attempt_start
        if node_exit == _NODE_RETRY_EXIT:
            if elapsed >= _RETRY_LONG_RUN_THRESHOLD:
                delay = _RETRY_BASE_DELAY
            log.info(
                "Jitsi connection lost (transient), reconnecting in %.0fs…",
                delay,
            )
            await asyncio.sleep(delay)
            if elapsed < _RETRY_LONG_RUN_THRESHOLD:
                delay = min(delay * 2, _RETRY_MAX_DELAY)
            attempt += 1
            continue

        break

    return exit_code


async def _read_puller_stdout(
    stdout: asyncio.StreamReader,
    orchestrator: SpeakerOrchestrator,
) -> None:
    while True:
        line = await stdout.readline()
        if not line:
            break
        try:
            text = line.decode("utf-8", errors="replace")
        except Exception:
            continue
        await orchestrator.handle_control_line(text)
