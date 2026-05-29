"""Tests for per-speaker caption helpers and orchestrator control handling."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

from captions_relay.pulse_captions import (
    SpeakerInfo,
    SpeakerOrchestrator,
    build_caption_body,
    echo_verbose_caption,
    format_speaker_line,
)


def test_format_speaker_line_with_name() -> None:
    assert format_speaker_line("Alice", "hello world") == "Alice: hello world"


def test_format_speaker_line_without_name() -> None:
    assert format_speaker_line(None, "hello world") == "hello world"


def test_echo_verbose_caption_final(capsys) -> None:
    speaker = SpeakerInfo(participant_id="p1", name="Alice")
    echo_verbose_caption("hello world", "final", speaker=speaker)
    assert capsys.readouterr().out == "Alice: hello world\n"


def test_echo_verbose_caption_partial(capsys) -> None:
    speaker = SpeakerInfo(participant_id="p1", name="Alice")
    echo_verbose_caption("hello", "partial", speaker=speaker)
    assert capsys.readouterr().out == "\rAlice: hello"


def test_build_caption_body_includes_speaker() -> None:
    speaker = SpeakerInfo(participant_id="p1", name="Alice")
    body = build_caption_body("hi", "final", speaker=speaker)
    assert body["text"] == "hi"
    assert body["kind"] == "final"
    assert body["speaker"] == {"id": "p1", "name": "Alice"}


def test_build_caption_body_omits_speaker_when_none() -> None:
    body = build_caption_body("hi", "partial")
    assert "speaker" not in body


async def _speaker_publish_prefixes_text() -> None:
    from captions_relay.pulse_captions import _speaker_publish

    ch = MagicMock()
    ch.publish = AsyncMock()
    speaker = SpeakerInfo(participant_id="p1", name="Alice")
    publish = _speaker_publish(ch, speaker)
    await publish({"text": "hello world", "kind": "final", "t": "now"})

    ch.publish.assert_awaited_once()
    payload = ch.publish.await_args.args[1]
    assert payload["text"] == "Alice: hello world"
    assert payload["speaker"] == {"id": "p1", "name": "Alice"}


def test_speaker_publish_prefixes_text() -> None:
    asyncio.run(_speaker_publish_prefixes_text())


async def _orchestrator_starts_pipeline_on_track_added() -> None:
    ch = MagicMock()
    ch.publish = AsyncMock()
    started: list[str] = []

    orch = SpeakerOrchestrator(
        ch=ch,
        shell_command_for_pipe=lambda pipe: f"echo {pipe}",
        line_kind="auto",
        debounce_ms=400,
        min_interval_ms=450,
        max_speakers=8,
        verbose_echo_whisper=False,
    )

    from captions_relay import pulse_captions as pc

    async def fake_start(self: pc.SpeakerPipelineTask) -> None:
        started.append(self.track_id)

    original_start = pc.SpeakerPipelineTask.start
    pc.SpeakerPipelineTask.start = fake_start  # type: ignore[method-assign]
    try:
        await orch.handle_control_line(
            '{"event":"track","action":"added","trackId":"t1","participantId":"p1",'
            '"name":"Alice","pipe":"/tmp/t1.pcm","sampleRate":48000}'
        )
    finally:
        pc.SpeakerPipelineTask.start = original_start  # type: ignore[method-assign]

    assert started == ["t1"]
    assert "t1" in orch._pipelines


def test_orchestrator_starts_pipeline_on_track_added() -> None:
    asyncio.run(_orchestrator_starts_pipeline_on_track_added())


async def _orchestrator_respects_max_speakers() -> None:
    ch = MagicMock()
    ch.publish = AsyncMock()

    orch = SpeakerOrchestrator(
        ch=ch,
        shell_command_for_pipe=lambda pipe: f"echo {pipe}",
        line_kind="auto",
        debounce_ms=400,
        min_interval_ms=450,
        max_speakers=1,
        verbose_echo_whisper=False,
    )

    from captions_relay import pulse_captions as pc

    original_start = pc.SpeakerPipelineTask.start

    async def fake_start(self: pc.SpeakerPipelineTask) -> None:
        self._task = asyncio.create_task(asyncio.sleep(3600))

    pc.SpeakerPipelineTask.start = fake_start  # type: ignore[method-assign]
    try:
        await orch.handle_control_line(
            '{"event":"track","action":"added","trackId":"t1","participantId":"p1",'
            '"name":"Alice","pipe":"/tmp/t1.pcm","sampleRate":48000}'
        )
        await orch.handle_control_line(
            '{"event":"track","action":"added","trackId":"t2","participantId":"p2",'
            '"name":"Bob","pipe":"/tmp/t2.pcm","sampleRate":48000}'
        )
    finally:
        pc.SpeakerPipelineTask.start = original_start  # type: ignore[method-assign]
        for pipeline in orch._pipelines.values():
            if pipeline._task is not None:
                pipeline._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pipeline._task

    assert list(orch._pipelines.keys()) == ["t1", "t2"]
    assert "t2" in orch._waiting_for_slot


def test_orchestrator_respects_max_speakers() -> None:
    asyncio.run(_orchestrator_respects_max_speakers())


async def _orchestrator_mute_stops_pipeline() -> None:
    ch = MagicMock()
    ch.publish = AsyncMock()
    orch = SpeakerOrchestrator(
        ch=ch,
        shell_command_for_pipe=lambda pipe: f"echo {pipe}",
        line_kind="auto",
        debounce_ms=400,
        min_interval_ms=450,
        max_speakers=8,
        verbose_echo_whisper=False,
    )

    pipeline = MagicMock()
    pipeline.stop = AsyncMock()
    orch._pipelines["t1"] = pipeline

    await orch.handle_control_line('{"event":"track","action":"mute","trackId":"t1","muted":true}')
    pipeline.stop.assert_awaited_once()
    assert "t1" in orch._muted_tracks


def test_orchestrator_mute_stops_pipeline() -> None:
    asyncio.run(_orchestrator_mute_stops_pipeline())


async def _orchestrator_removed_stops_pipeline() -> None:
    ch = MagicMock()
    ch.publish = AsyncMock()
    orch = SpeakerOrchestrator(
        ch=ch,
        shell_command_for_pipe=lambda pipe: f"echo {pipe}",
        line_kind="auto",
        debounce_ms=400,
        min_interval_ms=450,
        max_speakers=8,
        verbose_echo_whisper=False,
    )

    pipeline = MagicMock()
    pipeline.stop = AsyncMock()
    orch._pipelines["t1"] = pipeline

    await orch.handle_control_line('{"event":"track","action":"removed","trackId":"t1"}')
    pipeline.stop.assert_awaited_once()
    assert "t1" not in orch._pipelines


def test_orchestrator_removed_stops_pipeline() -> None:
    asyncio.run(_orchestrator_removed_stops_pipeline())


async def _orchestrator_promotes_waiting_on_remove() -> None:
    ch = MagicMock()
    ch.publish = AsyncMock()

    orch = SpeakerOrchestrator(
        ch=ch,
        shell_command_for_pipe=lambda pipe: f"echo {pipe}",
        line_kind="auto",
        debounce_ms=400,
        min_interval_ms=450,
        max_speakers=1,
        verbose_echo_whisper=False,
    )

    from captions_relay import pulse_captions as pc

    started: list[str] = []

    async def fake_start(self: pc.SpeakerPipelineTask) -> None:
        started.append(self.track_id)
        self._task = asyncio.create_task(asyncio.sleep(3600))

    original_start = pc.SpeakerPipelineTask.start
    pc.SpeakerPipelineTask.start = fake_start  # type: ignore[method-assign]
    try:
        await orch.handle_control_line(
            '{"event":"track","action":"added","trackId":"t1","participantId":"p1",'
            '"name":"Alice","pipe":"/tmp/t1.pcm","sampleRate":48000,"muted":false}'
        )
        await orch.handle_control_line(
            '{"event":"track","action":"added","trackId":"t2","participantId":"p2",'
            '"name":"Bob","pipe":"/tmp/t2.pcm","sampleRate":48000,"muted":false}'
        )
        assert started == ["t1"]
        assert "t2" in orch._waiting_for_slot

        await orch.handle_control_line('{"event":"track","action":"removed","trackId":"t1"}')
    finally:
        pc.SpeakerPipelineTask.start = original_start  # type: ignore[method-assign]
        for pipeline in orch._pipelines.values():
            if pipeline._task is not None:
                pipeline._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pipeline._task

    assert started == ["t1", "t2"]
    assert "t2" not in orch._waiting_for_slot


def test_orchestrator_promotes_waiting_on_remove() -> None:
    asyncio.run(_orchestrator_promotes_waiting_on_remove())
