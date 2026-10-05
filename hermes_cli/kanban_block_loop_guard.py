"""Block-loop guard for the auto-decomposer / auto-specifier.

``hermes_cli.kanban_db._route_block`` already detects the unblock↔re-block loop:
after ``BLOCK_RECURRENCE_LIMIT`` same-kind blocks it routes the task to
``triage`` (and emits ``block_loop_detected``) instead of letting a cron keep
unblocking it. That part is correct. The bug it left open is one layer up:
the *decomposer* and *specifier* read ``triage`` tasks and rewrite them
without checking whether the trip to ``triage`` was a ``block_loop_detected``
or an organic triage entry — so the moment a card's human-input blocker is
still unresolved, the dispatcher/spec loop restarts (3 re-specs in 9 minutes
was the failure mode that surfaced this; see task t_832840f6).

This module is the shared guard both surfaces call BEFORE the LLM round-trip:

    outcome = decompose_task(task_id) / specify_task(task_id)
    if not guard.is_block_loop_active(conn, task_id, lookback_seconds=...):
        ...   # refuse + surface + emit decompose_refused

A refusal:

  1. Appends a kanban comment from the auto-* author naming the most recent
     ``block_loop_detected`` event so the hub sees the refusal in-thread.
  2. Emits a structured ``decompose_refused`` (or ``specify_refused``) event
     so dashboard views and tests can detect refusals without scraping prose.
  3. Throttle-creates ONE ``assignee='default'`` hub triage card titled
     ``Hub: auto-decompose refused on <task_id> — block-loop active since <ts>``
     per task per 6 hours, so the dispatcher's per-tick cron does not file
     a fresh card on every sweep.

Two config knobs (``kanban.auto_decompose_respect_block_loop`` and
``kanban.auto_specify_respect_block_loop``) let an operator deliberately
allow a re-spec on a long-stale blocked card; ``kanban.block_loop_lookback_seconds``
(default 24h) bounds how recent a ``block_loop_detected`` must be to count.
The knobs are independent for decompose and specify so each path can be
loosened in isolation.

Edge cases:

  - ``block_kind`` on the task is NULL (organic triage): guard short-circuits
    to ``(False, {})`` — there is no loop to be in.
  - A ``commented`` event on the task since the most recent
    ``block_loop_detected`` is the "human re-engagement" signal
    (matches ``kanban_diagnostics._stuck_in_blocked``); guard returns False
    so the operator can hand the spec back to the LLM.
  - An ``unblocked`` event since the most recent ``block_loop_detected``
    is also a re-engagement signal — see ``kanban_db.unblock_task`` which
    emits ``unblocked`` only after an operator-/cron-driven transition.
  - The lookback window uses ``time.time()`` like every other timestamp in
    the row; an old ``block_loop_detected`` falls off and the operator
    explicitly re-arriving is the only way the LLM call resumes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


logger = logging.getLogger(__name__)


# How often (seconds) the guard may file the same "auto-decompose refused"
# hub card for the same task. Per-tick dispatch loop runs ~every 60s; 6h
# bounds one operator-action cycle.
HUB_TRIAGE_THROTTLE_SECONDS = 6 * 3600


@dataclass
class BlockLoopState:
    """Result of one guard check; empty when the guard is inactive.

    ``active`` True means: do NOT call the LLM; emit a refusal surface.
    ``event_id`` is the ``task_events.id`` of the most recent matching
    ``block_loop_detected``; ``detected_at`` is its ``created_at`` (unix sec);
    ``block_kind`` is the kind the loop was for (``None`` when legacy-typed);
    ``recurrences`` is the unblock-loop counter at detection time.
    """

    active: bool = False
    event_id: Optional[int] = None
    detected_at: Optional[int] = None
    block_kind: Optional[str] = None
    recurrences: int = 0


# Event kinds emitted by the refusal surface. Kept module-level so a
# dashboard view or test can import the constant without scraping prose.
DECOMPOSE_REFUSED_EVENT = "decompose_refused"
SPECIFY_REFUSED_EVENT = "specify_refused"
REFUSAL_REASON = "block_loop_active"


def _load_kanban_cfg() -> dict:
    """``kanban:`` block from the active profile config; empty dict on read
    failure (the call site wants to *not* silently enable the guard)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("block_loop_guard: config read failed: %s", exc)
        return {}
    if not isinstance(cfg, dict):
        return {}
    return cfg.get("kanban", {}) if isinstance(cfg.get("kanban"), dict) else {}


def get_decompose_respect_block_loop() -> bool:
    """``True`` (default) ⇒ refuse a decompose whose target is mid block-loop.
    ``False`` ⇒ proceed and let the LLM rewrite; surface a log line."""
    cfg = _load_kanban_cfg()
    val = cfg.get("auto_decompose_respect_block_loop", True)
    return bool(val) if isinstance(val, (bool, int)) else True


def get_specify_respect_block_loop() -> bool:
    """Symmetric to :func:`get_decompose_respect_block_loop` for the specifier."""
    cfg = _load_kanban_cfg()
    val = cfg.get("auto_specify_respect_block_loop", True)
    return bool(val) if isinstance(val, (bool, int)) else True


def get_block_loop_lookback_seconds() -> int:
    """``int`` window for a recent ``block_loop_detected`` to count as active."""
    cfg = _load_kanban_cfg()
    raw = cfg.get("block_loop_lookback_seconds", 24 * 3600)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 24 * 3600
    if v < 0:
        return 0
    return v


def check_block_loop(
    conn: sqlite3.Connection, task_id: str, *, lookback_seconds: Optional[int] = None,
) -> BlockLoopState:
    """Read ``task_events`` for a recent ``block_loop_detected`` matching the
    task's ``block_kind``, then verify no ``commented`` / ``unblocked`` event
    followed it.

    Returns ``BlockLoopState(active=False)`` when:

      - the task has no recent ``block_loop_detected`` (organic triage, or
        the loop fell off the lookback window), OR
      - a human has re-engaged via a ``commented`` or ``unblocked`` event
        since the most recent loop detection.

    Both kinds are checked for the "since" event because ``unblock_task`` in
    ``kanban_db.py`` does not add a comment on its own — only ``add_comment``
    does — and an operator pressing the dashboard's unblock button after the
    triage route is itself a re-engagement signal.

    Implementation note: one ``SELECT MAX(id) ... GROUP BY kind`` fetches
    every kind of interest in a single round-trip; for a triage task with a
    handful of historical events this is cheaper than three separate queries
    and never escapes the row the task has touched (the events table is
    task-scoped by ``task_id``).
    """
    if lookback_seconds is None:
        lookback_seconds = get_block_loop_lookback_seconds()
    if lookback_seconds < 0:
        lookback_seconds = 0

    row = conn.execute(
        "SELECT block_kind FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return BlockLoopState(active=False)
    task_block_kind = row["block_kind"] if isinstance(row, sqlite3.Row) else row[0]
    # ``block_kind`` NULL means the task was placed in triage organically —
    # no loop to refuse on.
    if not task_block_kind:
        return BlockLoopState(active=False)

    now = int(time.time())
    cutoff = now - lookback_seconds

    # Most recent block_loop_detected within the window; payload carries the
    # kind it was for. Untyped loops (None kind) match any stored block_kind.
    loop_row = conn.execute(
        "SELECT id, created_at, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'block_loop_detected' "
        "AND created_at >= ? ORDER BY id DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if loop_row is None:
        return BlockLoopState(active=False)

    payload_text = loop_row["payload"] if isinstance(loop_row, sqlite3.Row) else loop_row[2]
    payload: dict = {}
    if payload_text:
        try:
            parsed = json.loads(payload_text)
        except (ValueError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
    detected_kind = payload.get("kind") if isinstance(payload.get("kind"), str) else None
    # The block_loop_detected kind must match the task's current block_kind
    # (or both be NULL). Untyped tasks never reach here (NULL guard above).
    if detected_kind != task_block_kind:
        return BlockLoopState(active=False)

    detected_at = int(loop_row["created_at"]) if isinstance(loop_row, sqlite3.Row) else int(loop_row[1])
    # Any re-engagement event after the loop detection beats the guard.
    re_engaged = conn.execute(
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND kind IN ('commented', 'unblocked') "
        "AND created_at > ? AND id > ? LIMIT 1",
        (task_id, detected_at, int(loop_row["id"])),
    ).fetchone()
    if re_engaged is not None:
        return BlockLoopState(active=False)

    try:
        recurrences = int(payload.get("recurrences") or 0)
    except (TypeError, ValueError):
        recurrences = 0
    return BlockLoopState(
        active=True,
        event_id=int(loop_row["id"]),
        detected_at=detected_at,
        block_kind=detected_kind,
        recurrences=recurrences,
    )


def _recent_refusal_card_exists(conn: sqlite3.Connection, task_id: str, *, now: int,
                                throttle_seconds: int, hub_prefix: str) -> bool:
    """A hub-triage card titled with the same task_id already exists within
    the throttle window — skip the create so the per-tick cron does not
    file one every dispatch sweep."""
    cutoff = now - throttle_seconds
    needle = f"{hub_prefix}{task_id}"
    return conn.execute(
        "SELECT 1 FROM tasks WHERE title LIKE ? "
        "AND status NOT IN ('done', 'archived') "
        "AND created_at >= ? LIMIT 1",
        (f"{needle}%", cutoff),
    ).fetchone() is not None


def surface_refusal(
    *,
    task_id: str,
    state: BlockLoopState,
    surface: str,  # "decompose" | "specify"
    author: str,
    board: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Record the refusal: comment, event, throttled hub-triage card.

    Returns ``(hub_card_filed, hub_card_id, refusal_event_id)``:

      - ``hub_card_filed`` True when a fresh hub card was created this call.
      - ``hub_card_id`` the new task id (or ``None`` when throttled).
      - ``refusal_event_id`` the row id of the ``<surface>_refused`` event.

    All three are best-effort: a failure here must never strand the caller
    (the LLM refusal itself already returned). Errors are logged at INFO.
    """
    if surface not in ("decompose", "specify"):
        raise ValueError(f"surface must be 'decompose' or 'specify', got {surface!r}")
    event_kind = DECOMPOSE_REFUSED_EVENT if surface == "decompose" else SPECIFY_REFUSED_EVENT
    hub_prefix = "Hub: auto-decompose refused on " if surface == "decompose" else "Hub: auto-specify refused on "
    now = int(time.time())
    refusal_event_id: Optional[int] = None
    hub_card_id: Optional[str] = None
    hub_card_filed = False

    payload = {
        "reason": REFUSAL_REASON,
        "block_kind": state.block_kind,
        "recurrences": state.recurrences,
        "last_block_loop_at": state.detected_at,
        "loop_event_id": state.event_id,
    }

    # Best-effort comment + structured event. Failures here are logged but
    # never raised — the caller already chose to refuse.
    try:
        with kbc.connect_closing() as conn:
            with kb.write_txn(conn):
                kb.add_comment(
                    conn, task_id, author,
                    f"auto-{surface} refused: block-loop active for kind={state.block_kind!r} "
                    f"since event #{state.event_id} at {state.detected_at}; "
                    f"recurrences={state.recurrences}. Will retry once a human comment / "
                    "unblock is recorded or the lookback window elapses.",
                )
                cur = conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, ?, ?, ?)",
                    (task_id, event_kind, json.dumps(payload, ensure_ascii=False), now),
                )
                refusal_event_id = int(cur.lastrowid or 0)
    except Exception as exc:
        logger.info(
            "block_loop_guard: refused surface write failed for %s/%s: %s",
            task_id, surface, exc,
        )
        return False, None, None

    # Throttled hub-triage card; outside the comment/event txn so its own
    # write_txn doesn't nest.
    try:
        with kbc.connect_closing() as conn:
            if _recent_refusal_card_exists(
                conn, task_id, now=now,
                throttle_seconds=HUB_TRIAGE_THROTTLE_SECONDS, hub_prefix=hub_prefix,
            ):
                return False, None, refusal_event_id
            title = f"{hub_prefix}{task_id} — block-loop active since {state.detected_at}"
            body = (
                f"Auto-{surface} refused on {task_id} because the task's most recent "
                f"block_loop_detected is within the lookback window "
                f"(event #{state.event_id}, {state.detected_at}, kind={state.block_kind!r}, "
                f"recurrences={state.recurrences}). The dispatcher cron will skip this task "
                "until a human comment / unblock is recorded or the lookback elapses."
            )
            with kb.write_txn(conn):
                hub_card_id = kb.create_task(
                    conn,
                    title=title,
                    body=body,
                    assignee="default",
                    triage=False,
                    initial_status="running",
                    tenant=None,
                    board=board,
                    created_by=author,
                )
            hub_card_filed = True
    except Exception as exc:
        logger.info(
            "block_loop_guard: hub-triage card write failed for %s/%s: %s",
            task_id, surface, exc,
        )

    return hub_card_filed, hub_card_id, refusal_event_id


def maybe_refuse(
    *, task_id: str, surface: str, author: str,
) -> Optional[BlockLoopState]:
    """Top-level gate shared by ``decompose_task`` and ``specify_task``.

    Returns ``None`` when the call should proceed (no loop, or the operator
    has re-engaged). Returns a populated :class:`BlockLoopState` when the
    call MUST be refused — the surface code translates that into its own
    outcome dataclass and returns before the LLM round-trip.

    The shared-state write (comment + structured event + throttled hub card)
    is performed here so neither caller has to repeat the side-effect
    machinery. Failures inside the guard are FAIL-CLOSED: an exception is
    treated as ``active=True`` so the original spec-loop cannot be
    re-opened by a stale guard implementation. Callers should treat any
    non-``None`` return as an unconditional refusal.
    """
    if surface == "decompose":
        guard_enabled = get_decompose_respect_block_loop()
    elif surface == "specify":
        guard_enabled = get_specify_respect_block_loop()
    else:
        raise ValueError(f"surface must be 'decompose' or 'specify', got {surface!r}")
    if not guard_enabled:
        logger.info(
            "block_loop_guard: %s/%s proceeding with guard knob OFF", surface, task_id,
        )
        return None
    try:
        with kbc.connect_closing() as conn:
            state = check_block_loop(
                conn, task_id, lookback_seconds=get_block_loop_lookback_seconds(),
            )
    except Exception as exc:
        # Guard failed — fail closed. Surface a "guard error" refusal so the
        # comment trail captures the failure for an operator to investigate;
        # the surface code's own outcome carries the reason string.
        logger.warning(
            "block_loop_guard: %s/%s guard raised %s; refusing closed",
            surface, task_id, exc,
        )
        try:
            surface_refusal(
                task_id=task_id,
                state=BlockLoopState(),
                surface=surface,
                author=author,
            )
        except Exception:
            pass
        return BlockLoopState(active=True, block_kind="<guard_error>")
    if not state.active:
        return None
    try:
        surface_refusal(task_id=task_id, state=state, surface=surface, author=author)
    except Exception as exc:
        logger.info(
            "block_loop_guard: %s/%s surface_refusal raised %s",
            surface, task_id, exc,
        )
    return state