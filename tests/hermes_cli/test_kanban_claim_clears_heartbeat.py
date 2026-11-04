"""Tests: claiming a card clears ``tasks.last_heartbeat_at``.

``tasks.last_heartbeat_at`` belongs to the CURRENT run. It is written
only by that run's own worker (``heartbeat_worker``), and every release
path (``release_stale_claims``, the max-runtime kill, the reconcile of
an orphaned ``running`` row) already clears it to NULL when the run
ends.

The claim path was the one gap. ``_claim_and_open_run`` reset
``started_at`` to ``now`` but carried the PREVIOUS run's heartbeat
across the re-claim. The result is a card whose two liveness
timestamps disagree: a fresh start paired with a heartbeat that
describes a worker which is no longer on the card.

That combination is what Mission Control's ``card_stuck_4h``
detector calls an "orphan worker": ``started_at`` recent AND
``last_heartbeat_at`` older than the threshold. The detector then
reports the age of the STALE HEARTBEAT, so the alert says how long
the card has been "running" since a run that ended hours or days ago.

Live incident (kanban t_dba354a4, card t_d8d6c78f, 2026-11-04):

    claimed 2026-11-04 02:14:38Z   started_at    = 1793758478
    last heartbeat    2026-11-03 00:09:13Z   last_heartbeat_at = 1793664553
    alert fired       2026-11-04 02:15:14Z

The alert reported the card "running for 1566m (93961s)". It had been
running for 114 seconds.

The invariant under test:

    claim(card)  ->  tasks.last_heartbeat_at IS NULL

The first heartbeat of the new run replaces it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _make_task(conn, title="re-claim me", **kwargs):
    """Create a task row directly, mirroring what the CLI insert does."""
    import time

    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, assignee, priority, created_at, started_at, body
        ) VALUES (?, ?, 'ready', ?, 0, ?, ?, ?)
        """,
        (
            "t_reclaim",
            title,
            kwargs.get("assignee", "ultron"),
            now,
            kwargs.get("started_at", now),
            kwargs.get("body", ""),
        ),
    )
    assert cur.rowcount == 1
    conn.commit()
    return "t_reclaim"


def _claim(conn, task_id, now):
    """Run the real claim path the dispatcher uses."""
    return kb._claim_and_open_run(
        conn, task_id, "ready", "lock-1", now + 300, now
    )


def _set_heartbeat(conn, task_id, epoch):
    conn.execute(
        "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
        (epoch, task_id),
    )
    conn.commit()


def _read_heartbeat(conn, task_id):
    return conn.execute(
        "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["last_heartbeat_at"]


def test_claim_clears_a_heartbeat_left_by_a_previous_run(conn):
    """The regression: a re-claim must not inherit the old run's heartbeat.

    This is what produced the 2026-11-04 false "running for 1566m"
    critical alert for a card that had been running for 114 seconds.
    """
    task_id = _make_task(conn)
    first_now = 1_700_000_000

    # First run heartbeats, then the card is released back to ready.
    _claim(conn, task_id, first_now)
    _set_heartbeat(conn, task_id, first_now + 60)

    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
        "claim_expires = NULL WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    # Second claim, much later. The old heartbeat must not survive it.
    second_now = first_now + 26 * 3600
    run_id = _claim(conn, task_id, second_now)
    assert run_id is not None

    assert _read_heartbeat(conn, task_id) is None, (
        "last_heartbeat_at must be cleared on claim — a heartbeat from the "
        "previous run describes a worker that is no longer on the card"
    )


def test_claim_clears_a_stale_heartbeat_on_a_first_claim(conn):
    """Even a first claim clears a heartbeat left on the row.

    A card can carry a stale heartbeat without ever having run here —
    restored from a backup, moved between boards, or written by an
    older producer. The claim is the moment the run starts, so it is
    the moment the old heartbeat stops being meaningful.
    """
    task_id = _make_task(conn)
    _set_heartbeat(conn, task_id, 1_500_000_000)  # ~2 years old

    now = 1_700_000_000
    run_id = _claim(conn, task_id, now)
    assert run_id is not None

    assert _read_heartbeat(conn, task_id) is None


def test_new_run_heartbeat_replaces_the_cleared_value(conn):
    """After the claim clears it, the new run's heartbeat is recorded.

    The clear must not make the card permanently un-heartbeatable — the
    worker's own heartbeat has to land normally afterwards.
    """
    task_id = _make_task(conn)
    now = 1_700_000_000

    _claim(conn, task_id, now)
    assert _read_heartbeat(conn, task_id) is None

    _set_heartbeat(conn, task_id, now + 42)
    assert _read_heartbeat(conn, task_id) == now + 42


def test_claim_leaves_the_other_liveness_fields_alone(conn):
    """The fix touches only the heartbeat.

    ``started_at`` still tracks the current run, and the claim
    bookkeeping (status, lock, expiry, run row) is unchanged.
    """
    task_id = _make_task(conn, started_at=1_500_000_000)
    now = 1_700_000_000

    run_id = _claim(conn, task_id, now)
    assert run_id is not None

    row = conn.execute(
        "SELECT status, claim_lock, claim_expires, started_at, current_run_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "running"
    assert row["claim_lock"] == "lock-1"
    assert row["claim_expires"] == now + 300
    assert row["started_at"] == now
    assert row["current_run_id"] == run_id


def test_failed_claim_cas_does_not_clear_the_heartbeat(conn):
    """A failed CAS must not modify the row at all.

    The claim is a compare-and-set. When it loses, the card belongs to
    somebody else and its heartbeat is not ours to clear.
    """
    task_id = _make_task(conn)
    now = 1_700_000_000

    _claim(conn, task_id, now)
    _set_heartbeat(conn, task_id, now + 99)

    # Second claim while the first lock is still held -> CAS loses.
    result = _claim(conn, task_id, now + 10)
    assert result is None

    assert _read_heartbeat(conn, task_id) == now + 99
