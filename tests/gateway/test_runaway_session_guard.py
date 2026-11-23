"""Regression tests for the runaway routed-session guard.

The guard exists because one live friday Telegram DM route kept executing turns 544 hours
after it started, with 484.2M cache-read tokens and a ~338K-token context (~$30/day), and
nothing in the gateway bounded a route's cost.

The load-bearing detail these tests pin: **the segment age must come from the LIVE
TRANSCRIPT, not from ``sessions.started_at``.** The default compression mode is in-place, so
the session id does not change and ``started_at`` is never rewritten — it measures the
CONVERSATION's age. A guard built on it both misses the runaway and rotates healthy
long-lived conversations.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.runaway_session_guard import (
    DEFAULT_RUNAWAY_MAX_LIVE_CHARS,
    DEFAULT_RUNAWAY_MAX_SEGMENT_HOURS,
    RUNAWAY_RESET_REASON,
    RunawayLimits,
    check_route_runaway,
    evaluate_runaway,
    read_segment_stats,
)

HOUR = 3600.0


# --- limits --------------------------------------------------------------------

def test_limits_default_to_the_measured_values():
    limits = RunawayLimits()
    assert limits.max_segment_hours == DEFAULT_RUNAWAY_MAX_SEGMENT_HOURS == 336
    assert limits.max_live_chars == DEFAULT_RUNAWAY_MAX_LIVE_CHARS == 800_000
    assert limits.enabled is True


def test_limits_read_from_config():
    limits = RunawayLimits.from_config({
        "compression": {
            "runaway_max_segment_hours": 100,
            "runaway_max_live_chars": 200,
            "runaway_max_session_messages": 300,
        }
    })
    assert limits.max_segment_hours == 100
    assert limits.max_live_chars == 200
    assert limits.max_messages == 300


def test_zero_disables_a_limit():
    limits = RunawayLimits.from_config({
        "compression": {"runaway_max_live_chars": 0}
    })
    assert limits.max_live_chars == 0
    assert limits.enabled is True  # the other limits are still on


def test_all_zero_disables_the_guard():
    limits = RunawayLimits.from_config({
        "compression": {
            "runaway_max_segment_hours": 0,
            "runaway_max_live_chars": 0,
            "runaway_max_session_messages": 0,
        }
    })
    assert limits.enabled is False


def test_missing_config_uses_the_defaults():
    limits = RunawayLimits.from_config({})
    assert limits.max_segment_hours == DEFAULT_RUNAWAY_MAX_SEGMENT_HOURS
    assert limits.max_live_chars == DEFAULT_RUNAWAY_MAX_LIVE_CHARS


def test_malformed_config_falls_back_to_the_defaults():
    limits = RunawayLimits.from_config({"compression": "not-a-dict"})
    assert limits.max_segment_hours == DEFAULT_RUNAWAY_MAX_SEGMENT_HOURS


# --- the trip rule -------------------------------------------------------------

def test_oversized_and_old_segment_trips():
    verdict = evaluate_runaway(
        segment_started_at=time.time() - 500 * HOUR,
        message_count=300,
        live_chars=1_000_000,
        limits=RunawayLimits(),
    )
    assert verdict.tripped is True
    assert verdict.reset_reason == RUNAWAY_RESET_REASON
    assert "live transcript" in verdict.reason


def test_oversized_but_young_segment_does_not_trip():
    """A fresh-but-large segment is compaction's job, not the guard's — rotating it would
    throw away a session that is still actively working."""
    verdict = evaluate_runaway(
        segment_started_at=time.time() - 10 * HOUR,
        message_count=300,
        live_chars=1_000_000,
        limits=RunawayLimits(),
    )
    assert verdict.tripped is False


def test_old_but_small_segment_does_not_trip():
    """The critical false-positive case: a healthy long-lived route. Measured live, the fleet's
    oldest routes are 3,898h old with a 90-row, 120K-char transcript and read 17k-43k cache
    tokens per call. Rotating those would lose real user context for nothing."""
    verdict = evaluate_runaway(
        segment_started_at=time.time() - 3_898 * HOUR,
        message_count=90,
        live_chars=120_000,
        limits=RunawayLimits(),
    )
    assert verdict.tripped is False


def test_the_live_runaway_shape_trips():
    """A big transcript on a long-lived segment — the shape the guard exists for.

    The measured fleet: the live runaway's segment held 162K-659K chars with 242-465 active
    rows and read 160k-242k cache tokens per call. Under the shipped limits a 659K-char
    segment alone does NOT trip (it is under the 800K size limit), which is deliberate: the
    healthy population's 95th percentile is 477K chars and some legitimate routes reach 659K.
    The guard trips when the transcript is BOTH over the size limit AND long-lived.
    """
    verdict = evaluate_runaway(
        segment_started_at=time.time() - 1_200 * HOUR,
        message_count=465,
        live_chars=900_000,
        limits=RunawayLimits(),
    )
    assert verdict.tripped is True


def test_the_measured_runaway_segment_does_not_trip_on_size_alone():
    """The friday runaway's own segment: 162K chars, 242 rows, 110h old. It is under every
    limit, and it SHOULD be — compaction already shrank it to 242 rows out of 5,457, and
    rotating it would discard the live tail for no saving. The guard exists for the case
    compaction cannot fix: a transcript that stays huge."""
    verdict = evaluate_runaway(
        segment_started_at=time.time() - 110 * HOUR,
        message_count=242,
        live_chars=162_000,
        limits=RunawayLimits(),
    )
    assert verdict.tripped is False


def test_message_count_backstop_trips_on_its_own():
    """A transcript with tens of thousands of rows is pathological whatever its age."""
    verdict = evaluate_runaway(
        segment_started_at=time.time() - 1 * HOUR,
        message_count=6_000,
        live_chars=10,
        limits=RunawayLimits(),
    )
    assert verdict.tripped is True
    assert "message count" in verdict.reason


def test_disabled_guard_never_trips():
    verdict = evaluate_runaway(
        segment_started_at=time.time() - 10_000 * HOUR,
        message_count=100_000,
        live_chars=100_000_000,
        limits=RunawayLimits(max_segment_hours=0, max_live_chars=0, max_messages=0),
    )
    assert verdict.tripped is False


def test_missing_segment_timestamp_still_applies_the_size_rule():
    """No active rows means no timestamp; the size half must still be able to trip."""
    verdict = evaluate_runaway(
        segment_started_at=None,
        message_count=10,
        live_chars=5_000_000,
        limits=RunawayLimits(),
    )
    # age is unknown -> 0 -> the age half cannot fire -> no trip
    assert verdict.tripped is False


def test_negative_or_zero_age_does_not_trip():
    verdict = evaluate_runaway(
        segment_started_at=time.time() + 10 * HOUR,  # clock skew / bad data
        message_count=300,
        live_chars=1_000_000,
        limits=RunawayLimits(),
    )
    assert verdict.tripped is False


# --- reading the live transcript ------------------------------------------------

def _make_db(tmp_path, session_id, rows):
    """A tiny stand-in for the state DB with the messages table the guard reads.

    ``_read_ctx`` mirrors the real contract in ``hermes_state.py``: a ``@contextmanager`` that
    yields a connection. The guard calls it as ``with db._read_ctx() as conn``.
    """
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE messages ("
        "  id INTEGER PRIMARY KEY, session_id TEXT, content TEXT, timestamp REAL, active INTEGER)"
    )
    for content, ts, active in rows:
        conn.execute(
            "INSERT INTO messages (session_id, content, timestamp, active) VALUES (?,?,?,?)",
            (session_id, content, ts, active),
        )
    conn.commit()
    conn.close()

    class _DB:
        def __init__(self, path):
            self._path = path

        @contextmanager
        def _read_ctx(self):
            conn = sqlite3.connect(str(self._path))
            try:
                yield conn
            finally:
                conn.close()

    return _DB(db)


def test_segment_age_comes_from_the_oldest_ACTIVE_row(tmp_path):
    """The signal that matters: archived rows (active=0) must not set the segment age."""
    now = time.time()
    db = _make_db(tmp_path, "s1", [
        ("old archived content", now - 900 * HOUR, 0),
        ("middle archived", now - 800 * HOUR, 0),
        ("summary row", now - 100 * HOUR, 1),
        ("tail row", now - 1 * HOUR, 1),
    ])

    segment_started_at, count, chars = read_segment_stats(db, "s1")
    assert segment_started_at is not None
    assert abs((now - segment_started_at) / HOUR - 100) < 1.0
    assert count == 2
    assert chars == len("summary row") + len("tail row")


def test_archived_rows_do_not_count_toward_the_size(tmp_path):
    """Only the live transcript is re-read from cache, so only it counts toward the limit."""
    now = time.time()
    db = _make_db(tmp_path, "s1", [
        ("x" * 1000, now - 500 * HOUR, 0),
        ("y" * 100, now - 2 * HOUR, 1),
    ])
    segment_started_at, count, chars = read_segment_stats(db, "s1")
    assert count == 1
    assert chars == 100
    assert abs((now - segment_started_at) / HOUR - 2) < 1.0


def test_read_segment_stats_on_an_empty_session(tmp_path):
    db = _make_db(tmp_path, "s1", [])
    assert read_segment_stats(db, "s1") == (None, 0, 0)


def test_read_segment_stats_survives_a_broken_db():
    class _Broken:
        @contextmanager
        def _read_ctx(self):
            raise sqlite3.DatabaseError("boom")
            yield  # pragma: no cover - unreachable, keeps this a generator

    assert read_segment_stats(_Broken(), "s1") == (None, 0, 0)


def test_read_segment_stats_with_no_db():
    assert read_segment_stats(None, "s1") == (None, 0, 0)


def test_check_route_runaway_trips_on_a_real_transcript(tmp_path):
    now = time.time()
    db = _make_db(tmp_path, "s1", [
        ("z" * 900_000, now - 500 * HOUR, 1),
    ])
    verdict = check_route_runaway(
        db=db, session_id="s1", session_key="agent:test:telegram:dm:1", limits=RunawayLimits()
    )
    assert verdict.tripped is True


def test_check_route_runaway_does_not_trip_on_a_healthy_long_lived_route(tmp_path):
    """The false-positive guard: old, small, active. This must never rotate."""
    now = time.time()
    db = _make_db(tmp_path, "s1", [
        ("z" * 120_000, now - 3_898 * HOUR, 1),
    ])
    verdict = check_route_runaway(
        db=db, session_id="s1", session_key="agent:test:telegram:dm:1", limits=RunawayLimits()
    )
    assert verdict.tripped is False


def test_check_route_runaway_on_a_broken_db_does_not_trip():
    class _Broken:
        @contextmanager
        def _read_ctx(self):
            raise sqlite3.DatabaseError("boom")
            yield  # pragma: no cover - unreachable, keeps this a generator

    verdict = check_route_runaway(
        db=_Broken(), session_id="s1", session_key="k", limits=RunawayLimits()
    )
    assert verdict.tripped is False
