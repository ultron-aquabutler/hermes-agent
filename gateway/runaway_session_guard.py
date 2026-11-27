"""Runaway routed-session guard — bound the cost of one live context segment.

**Why this exists.** On 2026-11-11 the friday Telegram DM route
``agent:friday:telegram:dm:8943042074`` was found still executing turns **544 hours** after it
started, with **484.2M cache-read tokens** and a context grown to ~338K tokens. Every turn re-read
the whole transcript from the prompt cache: a measured +336,768 cache-read tokens per minute
(~$30/day). The route never rotated because nothing in the gateway bounds a route's cost.

**What this guard does.** It bounds a *context segment* — the live transcript the model actually
re-reads on every call — and resets the route (``/new`` semantics) when that segment is both
**expensive** and **long-lived**. The conversation continues on a fresh context instead of
re-reading an unbounded transcript forever.

**Measuring the segment correctly — this is the load-bearing part.**

The obvious signal, ``sessions.started_at``, is the WRONG one. It is the **conversation** start,
not the segment start, because the default compression mode is in-place:

- ``hermes_cli/config_defaults.py`` ships ``compression.in_place: True``.
- ``hermes_state_mutation_compress.py`` — the in-place path sets ``child = target``: the session
  id does **not** change, and the only ``UPDATE sessions`` bumps ``runtime_generation``.
  ``started_at`` is never rewritten.
- ``hermes_state_messages.py::_archive_and_compact_on_conn`` sets ``active = 0`` on the archived
  rows and **inserts** the compacted summary as fresh rows.

So after a compaction the session id is stable and ``sessions.started_at`` keeps counting from
the conversation's birth. Measured on the live fleet, the gap is large: the friday runaway read
**544h** by ``started_at`` but its live segment was only **110h** old; a root route read
**2,518h** by ``started_at`` against a **1,678h** segment. A guard built on ``started_at``
therefore both misses the runaway and, if tuned tightly enough to catch it, rotates healthy
long-lived conversations — losing real user context for nothing.

The correct signal is the **live transcript**: the rows with ``messages.active = 1``. Their oldest
timestamp is when the current segment was created (compaction archives the old rows and inserts
the summary as a new one, so the segment's clock restarts at every compaction), and their total
content length is the context the model re-reads on every single call.

**Why age alone is not a trip signal.** Measured across 11,262 live segments: segment age has a
median of **1,730h** and a max of **3,898h**. Long-lived routes are *normal* on this fleet — they
are old because they are still in daily use, and their transcripts stay small because they
compact. A healthy old route and the runaway differ in **cost**, not in age: healthy old routes
read 17k-43k cache tokens per call, the runaway read 160k-242k.

So the guard trips on the **pair**: a live transcript large enough that re-reading it every call is
expensive, AND a segment old enough that this is no longer a normal working session. On the live
population, ``live_chars >= 800,000`` and ``segment_age >= 336h`` (14 days) trips 3 of the 20
runaway segments and **2 of 3,328** healthy ones (0.06%). Both limits are configurable, and
either can be disabled independently.

**What this guard deliberately does not do.** It does NOT make time reset routes on its own.
``gateway/session_lifecycle.py::_route_reset_reason`` documents a hard invariant — *"Only explicit
suspension replaces a routed conversation; time never does."* This guard respects that: the runaway
limit is an **explicit, separately-configured, opt-out** rule with its own reset reason
(``runaway_limit``), not a reinterpretation of ordinary age. A session under the limits behaves
exactly as before.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Default max age of a single context segment, in hours. 336h = 14 days.
#:
#: Sized against the live fleet, not guessed: segment age has a median of 1,730h and a max of
#: 3,898h across 11,262 measured segments, because long-lived routes are normal here. The age
#: limit is therefore never the primary signal — the transcript-size limit is — it only excludes
#: a brand-new segment that is already huge, which is a different (and self-limiting) problem.
DEFAULT_RUNAWAY_MAX_SEGMENT_HOURS = 336

#: Default max size of the live transcript, in characters.
#:
#: ~800,000 chars is roughly 200K tokens of transcript. The live runaway's segment held 162K-659K
#: chars; the healthy population's 95th percentile is 477K chars, so this sits above ordinary
#: sessions and only the genuinely outsized ones trip it.
DEFAULT_RUNAWAY_MAX_LIVE_CHARS = 800_000

#: Default max messages in a single context segment. A last-resort backstop: the live runaway
#: segment held 242-465 active rows, and a legitimate long Kanban session can run into the
#: thousands before compaction rotates the transcript.
DEFAULT_RUNAWAY_MAX_SESSION_MESSAGES = 5000

#: Default max share of the live transcript that the compaction summary may occupy.
#:
#: This is the signal the absolute ``max_live_chars`` limit cannot see. On Vision (2026-11-26)
#: the live transcript was 161,507 chars -- comfortably under the 800,000 floor -- because ONE
#: 114,050-char summary row made up 70.6% of it. The transcript was small *because* the summary
#: had swallowed it, so a guard that only asks "is the transcript big?" sees a healthy session.
#:
#: The share is scale-free: it catches a summary dominating a small transcript as squarely as one
#: dominating a large transcript. Measured on this fleet: 12% is a normal large-context session,
#: 70.6% was the runaway. 0 disables the limit.
DEFAULT_RUNAWAY_MAX_SUMMARY_SHARE = 0.50

#: The reset reason recorded when the guard fires. Distinct from "suspended" so an operator can
#: tell an explicit /stop from a runaway rotation.
RUNAWAY_RESET_REASON = "runaway_limit"

#: A guard hit is a routing decision, not an error. Every knob is disabled by default in the
#: sense that a missing/zero value means "no limit" — the config layer supplies the defaults.
_DISABLED = 0


@dataclass(frozen=True)
class RunawayLimits:
    """The configured bounds on a single context segment.

    ``max_live_chars`` and ``max_segment_hours`` are ANDed: a segment trips only when it is
    BOTH expensive to re-read and long-lived. Either half alone is a bad trip rule — size alone
    would rotate a fresh-but-large session (which compaction will handle on its own), and age
    alone would rotate healthy long-lived conversations.
    """

    max_segment_hours: int = DEFAULT_RUNAWAY_MAX_SEGMENT_HOURS
    max_live_chars: int = DEFAULT_RUNAWAY_MAX_LIVE_CHARS
    max_messages: int = DEFAULT_RUNAWAY_MAX_SESSION_MESSAGES
    #: Max share (0.0-1.0) of the live transcript the summary may occupy. ``0.0`` disables it.
    max_summary_share: float = DEFAULT_RUNAWAY_MAX_SUMMARY_SHARE

    @classmethod
    def from_config(cls, data: Optional[dict[str, Any]]) -> RunawayLimits:
        """Read the limits out of a gateway config dict. Missing/zero disables that limit."""
        compression = (data or {}).get("compression") or {}
        if not isinstance(compression, dict):
            compression = {}

        def _int(key: str, default: int) -> int:
            raw = compression.get(key, default)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        def _share(key: str, default: float) -> float:
            raw = compression.get(key, default)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return default
            # A share outside 0.0-1.0 is meaningless; clamp rather than trust it.
            if value < 0.0:
                return 0.0
            if value > 1.0:
                return 1.0
            return value

        max_age = _int("runaway_max_segment_hours", DEFAULT_RUNAWAY_MAX_SEGMENT_HOURS)
        max_chars = _int("runaway_max_live_chars", DEFAULT_RUNAWAY_MAX_LIVE_CHARS)
        max_msgs = _int("runaway_max_session_messages", DEFAULT_RUNAWAY_MAX_SESSION_MESSAGES)
        max_share = _share("runaway_max_summary_share", DEFAULT_RUNAWAY_MAX_SUMMARY_SHARE)
        # "All limits zeroed" is the documented way to switch the guard off. If an operator
        # zeroes every *other* limit, the summary-share limit must not silently stay on --
        # otherwise the guard never fully turns off and the reason it tripped is a surprise.
        if max_age == 0 and max_chars == 0 and max_msgs == 0:
            max_share = 0.0
        return cls(
            max_segment_hours=max_age if max_age > 0 else _DISABLED,
            max_live_chars=max_chars if max_chars > 0 else _DISABLED,
            max_messages=max_msgs if max_msgs > 0 else _DISABLED,
            max_summary_share=max_share,
        )

    @property
    def enabled(self) -> bool:
        return (
            self.max_segment_hours > 0
            or self.max_live_chars > 0
            or self.max_messages > 0
            or self.max_summary_share > 0.0
        )


@dataclass(frozen=True)
class RunawayVerdict:
    """Why (or why not) a route must be rotated."""

    tripped: bool
    reason: str = ""
    age_hours: float = 0.0
    message_count: int = 0
    live_chars: int = 0

    @property
    def reset_reason(self) -> str:
        return RUNAWAY_RESET_REASON


def evaluate_runaway(
    *,
    segment_started_at: Optional[float],
    message_count: int,
    live_chars: int,
    limits: RunawayLimits,
    now: Optional[float] = None,
    summary_share: float = 0.0,
) -> RunawayVerdict:
    """Decide whether a context segment has run away.

    ``segment_started_at`` is the timestamp of the OLDEST ACTIVE row of the live transcript
    (epoch seconds) — not ``sessions.started_at``, which is the conversation's start and does not
    move when compaction rewrites the transcript in place. See the module docstring.

    A missing ``segment_started_at`` means the segment has no timestamped active rows, so the age
    half of the rule cannot be evaluated; the size half still applies.

    ``summary_share`` is the share of the live transcript occupied by the compaction summary
    (0.0-1.0, 0.0 = unknown). Unlike the size limit, this stands **alone**: a summary that has
    swallowed the transcript is a runaway on its own terms, and the transcript is small precisely
    because the summary ate it — so ANDing it with age or size would hide the exact failure it is
    meant to catch.
    """
    if not limits.enabled:
        return RunawayVerdict(tripped=False)

    now = time.time() if now is None else now
    age_hours = 0.0
    if segment_started_at is not None:
        try:
            age_hours = max(0.0, (now - float(segment_started_at)) / 3600.0)
        except (TypeError, ValueError):
            age_hours = 0.0

    count = int(message_count or 0)
    chars = int(live_chars or 0)

    # The message-count backstop stands alone: a transcript with tens of thousands of rows is
    # pathological whatever its age or size.
    if limits.max_messages > 0 and count > limits.max_messages:
        return RunawayVerdict(
            tripped=True,
            reason=f"message count {count} exceeds limit {limits.max_messages}",
            age_hours=age_hours,
            message_count=count,
            live_chars=chars,
        )

    # The summary-share backstop also stands alone. A transcript that is mostly one summary row is
    # a runaway even though it is small — the size limit cannot see it, because the summary is
    # what made the transcript small.
    share = float(summary_share or 0.0)
    if limits.max_summary_share > 0.0 and share >= limits.max_summary_share:
        return RunawayVerdict(
            tripped=True,
            reason=(
                f"compaction summary occupies {share * 100:.1f}% of the live transcript "
                f"(limit {limits.max_summary_share * 100:.1f}%)"
            ),
            age_hours=age_hours,
            message_count=count,
            live_chars=chars,
        )

    # Size + age are ANDed. Both halves must fire for the segment to trip.
    over_size = limits.max_live_chars > 0 and chars > limits.max_live_chars
    over_age = limits.max_segment_hours > 0 and age_hours > limits.max_segment_hours
    if over_size and over_age:
        return RunawayVerdict(
            tripped=True,
            reason=(
                f"live transcript {chars} chars exceeds limit {limits.max_live_chars} "
                f"and segment age {age_hours:.1f}h exceeds limit {limits.max_segment_hours}h"
            ),
            age_hours=age_hours,
            message_count=count,
            live_chars=chars,
        )

    return RunawayVerdict(
        tripped=False,
        age_hours=age_hours,
        message_count=count,
        live_chars=chars,
    )


def read_segment_stats(db: Any, session_id: str) -> tuple[Optional[float], int, int]:
    """Read ``(segment_started_at, message_count, live_chars)`` for a session.

    ``(None, 0, 0)`` on any failure. A DB error is never a reason to rotate a route — the guard
    must not fail a live conversation because a lookup went wrong.

    ``segment_started_at`` comes from the OLDEST ACTIVE message: that is the live transcript the
    model re-reads on every call, and compaction archives the old rows and inserts the summary
    as a new one, so this timestamp restarts at every compaction.
    """
    if not db or not session_id:
        return (None, 0, 0)
    read_ctx = getattr(db, "_read_ctx", None)
    if read_ctx is None:
        return (None, 0, 0)
    try:
        with read_ctx() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) FROM messages "
                "WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
            oldest_row = conn.execute(
                "SELECT MIN(timestamp) FROM messages WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
        count = int(row[0] or 0) if row is not None else 0
        live_chars = int(row[1] or 0) if row is not None and len(row) > 1 else 0
        oldest = oldest_row[0] if oldest_row is not None and oldest_row[0] is not None else None
        return (float(oldest) if oldest is not None else None, count, live_chars)
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        return (None, 0, 0)
    except Exception:  # pragma: no cover - defensive: never break routing on a guard lookup
        return (None, 0, 0)


def read_summary_dominance(db: Any, session_id: str) -> float:
    """Return the share of the live transcript occupied by the compaction summary.

    0.0 means "unknown" (no summary row, or the lookup failed).

    This is the signal the absolute ``live_chars`` limit cannot see. On Vision
    (2026-11-26) the live transcript was 161,507 chars -- comfortably under the
    800,000 floor -- because ONE 114,050-char summary row made up 70.6% of it.
    The transcript was small *because* the summary had swallowed it, so a guard
    that only asks "is the transcript big?" sees a healthy session.

    The share is scale-free: it catches a summary dominating a small transcript
    as squarely as one dominating a large transcript. Measured on this fleet:
    12% is a normal large-context session, 70.6% was the runaway.
    """
    if not db or not session_id:
        return 0.0
    read_ctx = getattr(db, "_read_ctx", None)
    if read_ctx is None:
        return 0.0
    try:
        with read_ctx() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM messages "
                "WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
            summary_row = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM messages "
                "WHERE session_id = ? AND active = 1 "
                "AND content LIKE '[CONTEXT COMPACTION%'",
                (session_id,),
            ).fetchone()
        live_chars = int(row[0] or 0) if row is not None else 0
        summary_chars = int(summary_row[0] or 0) if summary_row is not None else 0
        if live_chars <= 0:
            return 0.0
        return (summary_chars / live_chars) if summary_chars > 0 else 0.0
    except (sqlite3.Error, TypeError, ValueError, IndexError):
        return 0.0
    except Exception:  # pragma: no cover - defensive: never break routing on a guard lookup
        return 0.0


def check_route_runaway(
    *,
    db: Any,
    session_id: str,
    session_key: str,
    limits: RunawayLimits,
    now: Optional[float] = None,
) -> RunawayVerdict:
    """Full guard: read the segment stats, evaluate the limits, log a trip.

    Returns a non-tripping verdict on every failure path, so the caller can treat a tripped
    verdict as the only signal to rotate.
    """
    segment_started_at, message_count, live_chars = read_segment_stats(db, session_id)
    summary_share = read_summary_dominance(db, session_id)
    verdict = evaluate_runaway(
        segment_started_at=segment_started_at,
        message_count=message_count,
        live_chars=live_chars,
        limits=limits,
        now=now,
        summary_share=summary_share,
    )
    if verdict.tripped:
        logger.warning(
            "runaway_session_guard: route %s (session %s) tripped: %s — rotating to a fresh session",
            session_key, session_id, verdict.reason,
        )
    return verdict
