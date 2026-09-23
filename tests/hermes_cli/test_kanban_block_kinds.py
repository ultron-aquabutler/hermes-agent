"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------










def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Quarantine gate (t_8b48a01f, restored 2026-09-23 by t_efc7769a)
# ---------------------------------------------------------------------------
#
# When ``block_task`` trips BLOCK_RECURRENCE_LIMIT the row is quarantined:
# status -> triage AND hub_escalation -> 1 in the same write txn. The
# auto-decomposer / auto-specify paths then refuse to act on the card,
# breaking the ``block_loop_detected -> triage -> auto-specify -> ready
# -> worker blocks-again`` structural loop.
# ---------------------------------------------------------------------------


def test_block_loop_quarantine_sets_hub_escalation(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")

        task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.hub_escalation is True
    assert task.block_recurrences >= 2


def test_block_loop_quarantine_event_payload_includes_quarantine_flags(
    kanban_home: Path,
) -> None:
    """Payload fields ``quarantined`` + ``hub_escalation_set`` surface
    the new quarantine in any dashboard / metric filtering on them."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
    assert events
    payload = events[-1].payload or {}
    assert payload.get("quarantined") is True
    assert payload.get("hub_escalation_set") is True


def test_block_loop_quarantine_blocks_third_re_block(kanban_home: Path) -> None:
    """A third re-block must NOT clear hub_escalation — quarantine is
    persistent across manual unblock cycles, not one-shot."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        # Initial block -> unblock -> re-block trips the breaker.
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.hub_escalation is True

        # Operator manually unblocks (triage -> ???) and re-runs. Even if
        # block_recurrences stays at limit, the flag must persist so the
        # auto-decomposer keeps refusing the card.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', hub_escalation=1 WHERE id=?",
                (tid,),
            )
        kb.claim_task(conn, tid, claimer="worker")
        kb.block_task(conn, tid, reason="x", kind="capability")
        task = kb.get_task(conn, tid)
    assert task.hub_escalation is True


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# recompute_ready honors the unblock-loop breaker (t_5fe84da5)
# ---------------------------------------------------------------------------


def test_recompute_ready_does_not_promote_blocked_at_recurrence_limit(
    kanban_home: Path,
) -> None:
    """Regression for t_5fe84da5 — ``recompute_ready`` must honour the
    unblock-loop breaker's counter.

    Before the fix, a task in ``status='blocked'`` with
    ``block_recurrences >= BLOCK_RECURRENCE_LIMIT`` was still auto-promoted
    to ``ready`` by ``recompute_ready`` once its parents completed.  In
    the steady state ``block_task`` itself routes such rows to ``triage``
    and sets ``hub_escalation=1``, so ``recompute_ready`` never sees them
    through normal flow.  But a row can land in ``blocked`` with the
    counter at the limit via an operator SQL edit (clearing
    ``hub_escalation`` while leaving the row in ``blocked``), a recovery
    script that flipped status without zeroing the counter, or a future
    migration.  Without this gate those rows re-arm the structural loop:

        block → unblock → block (trip) → recompute_ready → ready
        → worker → block → unblock → block (trip) → …

    The promotion path must refuse regardless of *how* the row ended up
    here.
    """
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="loop-victim", assignee="worker")
        # Stand up a parent and complete it so the child gate is open.
        parent = kb.create_task(conn, title="parent", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        kb.complete_task(conn, parent, result="done")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        # Force the offending state directly.  The block_recurrences
        # counter is what block_task would have written on the trip wire.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_recurrences=?, "
                "block_kind='needs_input' WHERE id=?",
                (kb.BLOCK_RECURRENCE_LIMIT, child),
            )
        promoted = kb.recompute_ready(conn)
        task_after = kb.get_task(conn, child)
    assert promoted == 0, (
        "recompute_ready must not auto-promote a task past the unblock-loop "
        f"breaker; got promoted={promoted}, task={task_after}"
    )
    assert task_after is not None
    assert task_after.status == "blocked", (
        f"task at the breaker limit must remain blocked; got {task_after.status!r}"
    )
    # And the counter must be preserved — the breaker accumulates across
    # recovery cycles, just like consecutive_failures (#35072).
    assert task_after.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT


def test_recompute_ready_below_recurrence_limit_still_recovers(
    kanban_home: Path,
) -> None:
    """Counter one below the limit must NOT be a permanent sticky block.

    A legitimate unblock → re-block path that has not tripped the breaker
    yet (counter < LIMIT) must still recover when parents finish.  The
    new gate is per-counter, not a permanent ban.
    """
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="recoverer", assignee="worker")
        parent = kb.create_task(conn, title="parent", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        kb.complete_task(conn, parent, result="done")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_recurrences=?, "
                "block_kind='needs_input' WHERE id=?",
                (kb.BLOCK_RECURRENCE_LIMIT - 1, child),
            )
        promoted = kb.recompute_ready(conn)
        task_after = kb.get_task(conn, child)
    assert promoted == 1
    assert task_after is not None
    assert task_after.status == "ready"
    assert task_after.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT - 1


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


