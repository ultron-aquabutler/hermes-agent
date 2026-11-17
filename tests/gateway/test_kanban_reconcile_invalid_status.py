"""Tests: reconciliation of cards parked on a status outside the workflow.

A card can end up with a ``status`` the dispatcher does not understand —
``in_progress`` being the one seen in production (3 cards on the
``mcp-infra`` board, hidden from Mission Control's Kanban tab since
2026-11-03, and invisible to every dispatcher query).

Why nothing else finds it: every status-filtered query in the dispatcher
matches a literal. ``release_stale_claims`` and ``detect_crashed_workers``
match ``status = 'running'``; dispatch matches ``status = 'ready'``. A card
on ``in_progress`` matches none of them, so it can be neither dispatched,
nor completed (the transition guard requires a known source status), nor
reclaimed. ``reconcile_orphaned_running`` cannot see it either — it selects
``WHERE status = 'running'``. Only a raw ``SELECT`` finds such a card.

``reconcile_invalid_status`` is the backstop: it finds cards whose status is
outside ``VALID_STATUSES`` and moves them to a status the dispatcher
understands, so the value stops hiding the card from every view.

The write paths all validate (``create_task`` checks
``VALID_INITIAL_STATUSES``; the dashboard plugin whitelists status verbs), so
this is a BACKSTOP for values that arrived by direct SQL, a DB restore, or a
tool predating the workflow — not the primary fix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _force_status(conn, tid, status, **cols):
    """Force a task onto a status the validated write paths would reject.

    This is how the card got into the bad state in production: the value
    bypassed every validated path (direct SQL / restore / older tool).
    """
    sets = ["status=?"]
    vals = [status]
    for k, v in cols.items():
        sets.append(f"{k}=?")
        vals.append(v)
    conn.execute(
        f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*vals, tid),
    )
    conn.commit()


def _status(conn, tid):
    return conn.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,),
    ).fetchone()["status"]


def _claim_with_run(conn, tid, *, force=False):
    """Put the card in ``running`` with a real ``task_runs`` row.

    ``claim_task`` only transitions ``ready -> running`` (and refuses while a
    parent is unfinished), so the card has to be staged into ``ready`` first.
    ``force=True`` opens the run row directly, bypassing the parent gate —
    used to reproduce a card that DID run and then got parked on an invalid
    status while a parent was still open.
    Returns the run id.
    """
    _force_status(conn, tid, "ready")
    if force:
        now = int(__import__("time").time())
        lock = kb._claimer_id()
        expires = now + 3600
        run_cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, step_key, status, "
            "claim_lock, claim_expires, max_runtime_seconds, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
            (tid, "w", None, lock, expires, None, now),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id=?, worker_pid=NULL WHERE id=?",
            (run_id, tid),
        )
        conn.commit()
        return run_id
    run_id = kb.claim_task(conn, tid)
    assert run_id is not None, "claim_task failed — card was not claimable"
    return run_id


class TestReconcileInvalidStatus:
    def test_in_progress_card_is_repaired(self, conn):
        """The production case: a card parked on ``in_progress`` is found
        and moved to a status the dispatcher understands."""
        tid = kb.create_task(conn, title="stuck in progress", assignee="w")
        _force_status(conn, tid, "in_progress")

        repaired = kbd.reconcile_invalid_status(conn)

        assert repaired == [tid]
        assert _status(conn, tid) in kb.VALID_STATUSES

    def test_in_progress_card_becomes_blocked_without_run_history(self, conn):
        """No run at all → blocked, NOT auto-completed.

        There is no evidence the work was finished and no owner to infer a
        disposition from, so the sweep must not invent one. Auto-requeueing
        would put a card nobody wrote into the spawn path; auto-completing
        would fabricate a result.
        """
        tid = kb.create_task(conn, title="no run", assignee="w")
        _force_status(conn, tid, "in_progress")

        kbd.reconcile_invalid_status(conn)

        assert _status(conn, tid) == "blocked"

    def test_completed_run_becomes_done(self, conn):
        """A card whose last run completed is finished work → done."""
        tid = kb.create_task(conn, title="finished", assignee="w")
        _claim_with_run(conn, tid)
        kb._end_run(conn, tid, outcome="completed", status="completed")
        _force_status(conn, tid, "in_progress")

        kbd.reconcile_invalid_status(conn)

        assert _status(conn, tid) == "done"

    def test_ended_non_completed_run_becomes_ready(self, conn):
        """A run that ended without completing = unfinished work → ready,
        so the dispatcher can pick it up."""
        tid = kb.create_task(conn, title="unfinished", assignee="w")
        _claim_with_run(conn, tid)
        kb._end_run(conn, tid, outcome="crashed", status="crashed")
        _force_status(conn, tid, "in_progress")

        kbd.reconcile_invalid_status(conn)

        assert _status(conn, tid) == "ready"

    def test_ready_downgraded_to_todo_when_parents_unfinished(self, conn):
        """``ready`` must not be granted while a parent is unfinished, or the
        dispatcher spawns a child whose upstream work has not completed."""
        parent = kb.create_task(conn, title="parent", assignee="w")
        child = kb.create_task(conn, title="child", assignee="w")
        # Link the unfinished parent FIRST, so the parent gate is in force
        # when the card is claimed and when it is repaired.
        kb.link_tasks(conn, parent, child)
        # The card cannot be claimed through the normal path (claim_task
        # refuses while a parent is unfinished), so give it the run history
        # that the sweep reads to decide the disposition: a run that ended
        # without completing = unfinished work = would-be ``ready``.
        _claim_with_run(conn, child, force=True)
        kb._end_run(conn, child, outcome="crashed", status="crashed")
        _force_status(conn, child, "in_progress")

        kbd.reconcile_invalid_status(conn)

        assert _status(conn, child) == "todo"

    def test_live_worker_pid_defers_reconcile(self, conn):
        """A worker on this host that is still alive owns the card, whatever
        its status says — flipping it would race the live worker."""
        tid = kb.create_task(conn, title="maybe-alive", assignee="w")
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            _force_status(conn, tid, "in_progress", worker_pid=sleeper.pid)
            assert kbd.reconcile_invalid_status(conn) == []
            assert _status(conn, tid) == "in_progress"
        finally:
            sleeper.terminate()
            sleeper.wait()

    def test_dead_worker_pid_is_reconciled(self, conn):
        """An invalid-status card with a recorded but dead PID is repaired."""
        tid = kb.create_task(conn, title="dead-pid", assignee="w")
        dead = subprocess.Popen(["true"])
        dead.wait()
        _force_status(conn, tid, "in_progress", worker_pid=dead.pid)

        assert kbd.reconcile_invalid_status(conn) == [tid]

    def test_valid_statuses_untouched(self, conn):
        """Cards on statuses the workflow knows are left completely alone."""
        tids = []
        for status in ("todo", "ready", "blocked", "done", "running",
                       "review", "archived", "scheduled"):
            tid = kb.create_task(conn, title=f"s-{status}", assignee="w")
            tids.append(tid)
            _force_status(conn, tid, status)

        assert kbd.reconcile_invalid_status(conn) == []
        for tid in tids:
            assert _status(conn, tid) in kb.VALID_STATUSES

    def test_reconciled_event_and_note_logged(self, conn):
        """The repair must be auditable: a comment and a ``reconciled``
        event naming the offending status."""
        tid = kb.create_task(conn, title="audited", assignee="w")
        _force_status(conn, tid, "in_progress")

        kbd.reconcile_invalid_status(conn)

        events = kb.list_events(conn, tid)
        recon = [e for e in events if e.kind == "reconciled"]
        assert len(recon) == 1
        assert recon[0].payload["reason"] == "invalid_status"
        assert recon[0].payload["invalid_status"] == "in_progress"
        comments = kb.list_comments(conn, tid)
        assert any("in_progress" in (c.body or "") for c in comments)

    def test_sweep_is_idempotent(self, conn):
        """A second pass finds nothing left to repair."""
        tid = kb.create_task(conn, title="twice", assignee="w")
        _force_status(conn, tid, "in_progress")

        first = kbd.reconcile_invalid_status(conn)
        second = kbd.reconcile_invalid_status(conn)

        assert first == [tid]
        assert second == []

    def test_leaked_open_run_is_closed(self, conn):
        """The invariant ``current_run_id IS NULL <=> run row terminal`` must
        hold after the flip, or the card stays half-owned."""
        tid = kb.create_task(conn, title="leaked run", assignee="w")
        _claim_with_run(conn, tid)
        _force_status(conn, tid, "in_progress")

        kbd.reconcile_invalid_status(conn)

        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (tid,),
        ).fetchone()
        assert row["current_run_id"] is None

    def test_other_invalid_statuses_also_repaired(self, conn):
        """The sweep is status-agnostic: any value outside the workflow is
        handled, not just ``in_progress``."""
        for status in ("cancelled", "pending", "completed"):
            tid = kb.create_task(conn, title=f"bad-{status}", assignee="w")
            _force_status(conn, tid, status)

        repaired = kbd.reconcile_invalid_status(conn)

        assert len(repaired) == 3
        for tid in repaired:
            assert _status(conn, tid) in kb.VALID_STATUSES


class TestDispatchOnceReconcilesInvalidStatus:
    def test_dispatch_once_reconciles_invalid_status(self, conn):
        """The sweep is wired into the dispatcher tick, not just callable."""
        tid = kb.create_task(conn, title="hidden card", assignee="w")
        _force_status(conn, tid, "in_progress")

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (True, ""),
                                  dry_run=True)

        assert tid in result.reconciled_invalid_status
        assert _status(conn, tid) in kb.VALID_STATUSES

    def test_dispatch_once_reconcile_can_be_disabled(self, conn):
        """kanban.reconcile_orphans=false skips the pass entirely."""
        tid = kb.create_task(conn, title="hidden card", assignee="w")
        _force_status(conn, tid, "in_progress")

        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: (True, ""),
                                  dry_run=True, reconcile_orphans=False)

        assert result.reconciled_invalid_status == []
        assert _status(conn, tid) == "in_progress"
