"""Tests for `WhisperStdoutStreamProcessor` (pulse Auto line-kind segmentation)."""

from __future__ import annotations

import pytest

from captions_relay.pulse_captions import WhisperStdoutStreamProcessor


def test_auto_newline_only_emits_final() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="auto")
    assert p.feed(b"hello\n") == [("hello", "final")]
    assert p.close() == []


def test_auto_crlf_emits_final_not_empty() -> None:
    """``hello\\r\\n`` must not normalize the segment to \"\" (CR-before-LF used to break finals)."""
    p = WhisperStdoutStreamProcessor(line_kind="auto")
    assert p.feed(b"hello\r\n") == [("hello", "final")]


def test_auto_multiline_chunks() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="auto")
    assert p.feed(b"a\nb") == [("a", "final"), ("b", "partial")]
    assert p.feed(b"\nc\n") == [("b", "final"), ("c", "final")]
    assert p.close() == []


def test_auto_carriage_return_segment_then_final() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="auto")
    events = p.feed(b"HEL\rWORLD\n")
    assert events == [("WORLD", "partial"), ("WORLD", "final")]


def test_auto_utf8_waits_until_code_point_complete() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="auto")
    s = "\u00e9clair\n".encode("utf-8")
    assert p.feed(s[:1]) == []
    assert p.feed(s[1:]) == [("éclair", "final")]


def test_auto_chunk_after_carriage_return() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="auto")
    assert p.feed(b"HEL\r") == []
    assert p.feed(b"WORLD\n") == [
        ("WORLD", "partial"),
        ("WORLD", "final"),
    ]


def test_auto_close_flushes_partial_tail_as_final() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="auto")
    assert p.feed(b"last line,no newline,char") == [("last line,no newline,char", "partial")]
    assert p.close() == [("last line,no newline,char", "final")]


def test_forced_final_newline_chunks() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="final")
    assert p.feed(b"a\n") == [("a", "final")]
    assert p.feed(b"b") == []
    assert p.close() == [("b", "final")]


def test_forced_partial_mode() -> None:
    p = WhisperStdoutStreamProcessor(line_kind="partial")
    assert p.feed(b"z\n") == [("z", "partial")]
    assert p.close() == []


def test_invalid_line_kind_raises() -> None:
    with pytest.raises(ValueError, match="line_kind"):
        WhisperStdoutStreamProcessor(line_kind="bogus")
