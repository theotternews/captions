"""Signal-triggered caption sessions.

Runs ``signal-cli -a <ACCOUNT> jsonRpc`` as a child process and reacts to direct
messages from trusted senders. Commands must be prefixed with ``captions``:

- ``captions start <jitsi link>`` / ``captions restart <jitsi link>`` -- mint tokens,
  reply with the subscriber link, and launch the existing Jitsi pipeline.
- ``captions stop`` -- tear the current session down.
- ``captions status`` -- report whether a session is running.

signal-cli only makes outbound connections to Signal's servers, so this needs no
router port forwarding and grants no inbound shell access.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from urllib.parse import urlparse

from ably.sync.util.exceptions import AblyException

from captions_relay import session_cache
from captions_relay.ably_tokens import (
    mint_publisher_token,
    mint_subscriber_token,
    publish_session_end_marker,
)
from captions_relay.config import (
    normalize_caption_channel,
    subscriber_index_url,
    validate_jitsi_url,
)

_URL_RE = re.compile(r"https://\S+", re.IGNORECASE)
_PREFIX = "captions"
_START_WORDS = {"start"}
_RESTART_WORDS = {"restart"}
_STOP_WORDS = {"stop", "end"}
_STATUS_WORDS = {"status"}


def _log(msg: str) -> None:
    print(f"[signal] {msg}", file=sys.stderr, flush=True)


def _room_name_from_jitsi_url(url: str) -> str | None:
    """Lowercased room name from a Jitsi URL (mirrors the CLI's session-new rule)."""
    try:
        parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
        if not parts:
            return None
        if parts[0] == "moderated" and len(parts) >= 2:
            return parts[-1].lower()
        return "/".join(parts).lower()
    except Exception:
        return None


def _channel_for_jitsi_url(url: str) -> str:
    """Derive ``captions:<room>`` from a Jitsi URL, validated as an Ably channel."""
    room = _room_name_from_jitsi_url(url)
    if not room:
        raise ValueError("Could not determine a room name from the Jitsi URL.")
    return normalize_caption_channel(f"captions:{room}")


class SignalListener:
    """Owns the signal-cli subprocess and one caption session per trusted sender."""

    def __init__(
        self,
        *,
        account: str,
        signal_cli_bin: str,
        allowed_senders: set[str],
        allowed_jitsi_hosts: set[str],
        ttl_seconds: int,
        captions_bin: str = "captions",
        max_speakers: int = 8,
        verbose: bool = False,
        allow_self: bool = False,
    ) -> None:
        self.account = account
        self.signal_cli_bin = signal_cli_bin
        self.allowed_senders = allowed_senders
        self.allowed_jitsi_hosts = allowed_jitsi_hosts
        self.ttl_seconds = ttl_seconds
        self.captions_bin = captions_bin
        self.max_speakers = max_speakers
        self.verbose = verbose
        self.allow_self = allow_self

        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        # Epoch ms at startup; messages sent earlier (offline backlog) are ignored.
        self._started_at_ms = 0.0
        # One active session per sender: sender -> {channel, url, proc, watcher}.
        self._sessions: dict[str, dict] = {}
        # Last Jitsi URL seen per sender, so 'restart' can omit the link.
        self._last_url: dict[str, str] = {}

    async def run(self) -> int:
        """Start signal-cli and process incoming messages until it exits."""
        self._started_at_ms = time.time() * 1000
        _log(
            f"starting signal-cli for account {self.account} "
            f"(allowed senders: {', '.join(sorted(self.allowed_senders)) or 'NONE'}"
            f"{'; self-initiation enabled' if self.allow_self else ''})"
        )
        if not self.allowed_senders and not self.allow_self:
            _log(
                "WARNING: no trusted senders configured; every request will be ignored. "
                "Set CAPTIONS_SIGNAL_ALLOWED_SENDERS (or use --allow-self for testing)."
            )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.signal_cli_bin,
                "-a",
                self.account,
                "jsonRpc",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            _log(
                f"could not run {self.signal_cli_bin!r}: {e}. Install signal-cli or set "
                "CAPTIONS_SIGNAL_CLI_BIN."
            )
            return 127

        assert self._proc.stdout is not None
        stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                await self._handle_line(line)
        finally:
            stderr_task.cancel()
            for sender in list(self._sessions):
                await self._teardown_session_for(sender, reason="listener shutting down")

        rc = await self._proc.wait()
        _log(f"signal-cli exited with code {rc}")
        return rc

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        async for raw in self._proc.stderr:
            if self.verbose:
                _log(f"signal-cli: {raw.decode('utf-8', 'replace').rstrip()}")

    async def _handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or msg.get("method") != "receive":
            return
        params = msg.get("params") or {}
        envelope = params.get("envelope") or {}
        source = envelope.get("sourceNumber") or envelope.get("source")
        data_message = envelope.get("dataMessage") or {}
        text = data_message.get("message")
        sender = source
        msg_ts = envelope.get("timestamp")

        # Only act on direct (1:1) messages; ignore group chats.
        if text is not None and data_message.get("groupInfo"):
            return

        # Messages you send yourself arrive as sync transcripts, not dataMessages.
        # Only consider them when self-initiation is enabled (testing).
        if text is None and self.allow_self:
            sent_message = (envelope.get("syncMessage") or {}).get("sentMessage") or {}
            if sent_message.get("message") is not None and not sent_message.get("groupInfo"):
                text = sent_message.get("message")
                sender = self.account
                msg_ts = sent_message.get("timestamp") or msg_ts

        if not sender or text is None:
            return

        # Ignore the offline backlog signal-cli delivers on connect: only act on
        # messages sent after this listener started.
        if isinstance(msg_ts, (int, float)) and msg_ts < self._started_at_ms:
            _log(f"ignoring message from {sender} sent before startup")
            return

        is_self = sender == self.account
        if is_self:
            if not self.allow_self:  # loop protection
                return
        elif sender not in self.allowed_senders:
            _log(f"ignoring message from untrusted sender {sender}")
            return

        await self._dispatch(sender, str(text).strip())

    async def _dispatch(self, sender: str, text: str) -> None:
        tokens = text.split()
        if not tokens or tokens[0].lower() != _PREFIX:
            _log(f"ignoring message without '{_PREFIX}' prefix from {sender}: {text!r}")
            return

        verb = tokens[1].lower() if len(tokens) > 1 else ""
        url_match = _URL_RE.search(text)
        url = url_match.group(0).rstrip(".,);") if url_match else None

        if verb in _STATUS_WORDS:
            await self._reply(sender, self._status_text(sender))
            return
        if verb in _STOP_WORDS:
            if sender not in self._sessions:
                await self._reply(sender, "You have no caption session running.")
            else:
                await self._teardown_session_for(sender, reason=f"stop requested by {sender}")
                await self._reply(sender, "Captions stopped.")
            return
        if verb in _START_WORDS:
            if url is None:
                await self._reply(sender, f"Usage: {_PREFIX} start <https://meet.jit.si/Room>")
                return
            await self._start_session(sender, url, restart=False)
            return
        if verb in _RESTART_WORDS:
            if url is None:
                url = self._last_url.get(sender)
                if url is None:
                    await self._reply(
                        sender,
                        "No previous meeting to restart. Send "
                        f"'{_PREFIX} start <https://meet.jit.si/Room>' first.",
                    )
                    return
            await self._start_session(sender, url, restart=True)
            return

        await self._reply(
            sender,
            f"Unknown command. Use: '{_PREFIX} start <jitsi link>', "
            f"'{_PREFIX} restart', '{_PREFIX} stop', or '{_PREFIX} status'.",
        )

    async def _start_session(self, sender: str, url: str, *, restart: bool) -> None:
        # 'restart' reuses still-valid cached tokens so already-shared subscriber links
        # keep working; 'start' always mints fresh tokens.
        reuse_tokens = restart
        try:
            url = validate_jitsi_url(url, allowed_hosts=self.allowed_jitsi_hosts)
            channel = _channel_for_jitsi_url(url)
        except ValueError as e:
            await self._reply(sender, f"Cannot start captions: {e}")
            return

        # One session per sender: replace any existing one for this sender. Only signal
        # "ended" to old subscribers when the room actually changes.
        existing = self._sessions.get(sender)
        if existing is not None:
            await self._teardown_session_for(
                sender,
                reason=f"replaced by new request from {sender}",
                publish_end=existing["channel"] != channel,
            )

        subscriber_token: str | None = None
        if reuse_tokens:
            cached = session_cache.load_session(channel)
            if cached is not None and session_cache.is_valid(cached):
                subscriber_token = cached.get("subscriber_token")

        if subscriber_token is None:
            try:
                pub = mint_publisher_token(channel, self.ttl_seconds)
                sub = mint_subscriber_token(channel, self.ttl_seconds)
            except (AblyException, ValueError) as e:
                await self._reply(sender, f"Could not mint Ably tokens: {e}")
                return
            session_cache.save_session(channel, pub, sub)
            subscriber_token = sub.token

        try:
            proc = await asyncio.create_subprocess_exec(
                self.captions_bin,
                "whisper",
                "jitsi",
                "--channel",
                channel,
                "--jitsi-url",
                url,
                "--reconnect",
                "--max-speakers",
                str(self.max_speakers),
                *(("-v",) if self.verbose else ()),
            )
        except FileNotFoundError as e:
            await self._reply(
                sender,
                f"Tokens ready but could not launch the captions pipeline "
                f"({self.captions_bin!r}): {e}",
            )
            return

        watcher = asyncio.create_task(self._watch_session(sender, channel, proc))
        self._sessions[sender] = {
            "channel": channel,
            "url": url,
            "proc": proc,
            "watcher": watcher,
        }
        self._last_url[sender] = url
        verb = "restarted" if restart else "started"
        _log(f"{verb} session on {channel} for {url} (sender {sender}, pid {proc.pid})")

        if restart:
            room = _room_name_from_jitsi_url(url) or channel
            await self._reply(sender, f"Restarted captions for {room}.")
            return

        sub_url = subscriber_index_url(channel)
        await self._reply(
            sender,
            "Captions starting.\n"
            f"Channel: {channel}\n"
            f"Subscriber link: {sub_url}\n"
            f"Send '{_PREFIX} stop' to end or '{_PREFIX} restart' to reconnect.\n"
            "Subscriber token in the message below.",
        )
        await self._reply(sender, subscriber_token)

    async def _watch_session(
        self, sender: str, channel: str, proc: asyncio.subprocess.Process
    ) -> None:
        rc = await proc.wait()
        # Only clear if this sender's session still points at this process.
        session = self._sessions.get(sender)
        if session is not None and session.get("proc") is proc:
            _log(f"session on {channel} (sender {sender}) exited with code {rc}")
            del self._sessions[sender]

    async def _teardown_session_for(
        self, sender: str, *, reason: str, publish_end: bool = True
    ) -> None:
        session = self._sessions.pop(sender, None)
        if session is None:
            return
        channel = session["channel"]
        proc = session["proc"]
        watcher = session.get("watcher")
        if watcher is not None:
            watcher.cancel()

        _log(f"stopping session on {channel} (sender {sender}; {reason})")
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

        if publish_end:
            try:
                publish_session_end_marker(channel)
            except (AblyException, ValueError) as e:
                _log(f"failed to publish session-end marker for {channel}: {e}")

    def _status_text(self, sender: str) -> str:
        session = self._sessions.get(sender)
        if session is None:
            return "You have no caption session running."
        return (
            "Caption session running.\n"
            f"Channel: {session['channel']}\n"
            f"Jitsi: {session['url']}"
        )

    async def _reply(self, recipient: str, message: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        self._req_id += 1
        req = {
            "jsonrpc": "2.0",
            "method": "send",
            "id": str(self._req_id),
            "params": {"recipient": [recipient], "message": message},
        }
        try:
            self._proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            _log(f"failed to send reply to {recipient}: {e}")


async def run_signal_listener(
    *,
    account: str,
    signal_cli_bin: str,
    allowed_senders: set[str],
    allowed_jitsi_hosts: set[str],
    ttl_seconds: int,
    captions_bin: str = "captions",
    max_speakers: int = 8,
    verbose: bool = False,
    allow_self: bool = False,
) -> int:
    """Construct and run a :class:`SignalListener` to completion."""
    listener = SignalListener(
        account=account,
        signal_cli_bin=signal_cli_bin,
        allowed_senders=allowed_senders,
        allowed_jitsi_hosts=allowed_jitsi_hosts,
        ttl_seconds=ttl_seconds,
        captions_bin=captions_bin,
        max_speakers=max_speakers,
        verbose=verbose,
        allow_self=allow_self,
    )
    return await listener.run()
