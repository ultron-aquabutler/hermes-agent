"""Tests: claiming a card resets ``tasks.started_at`` to the current run.

``tasks.started_at`` must mark the start of the CURRENT run, not the
creation of the card. It used to be written as
``COALESCE(started_at, ?)`` on claim, which set it only when NULL — and
since ``started_at`` was written once at task creation and never
cleared, every re-claim of an old card kept its original creation
timestamp.

That is actively harmful downstream. Mission Control's ``card_stuck_4h``
detector measures run age from ``COALESCE(started_at, created_at)``, so
such a card satisfied "running for more than 4h" the instant it was
claimed, no matter how fresh its heartbeat was. On 2026-10-29 that
produced 57 critical Telegram alerts in 24 hours across 19 cards.

The invariant under test:

    claim(card)  ->  tasks.started_at == now  ==  task_runs.started_at
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


def test_first_claim_sets_started_at(conn):
    """The first claim sets started_at to now."""
    task_id = _make_task(conn)
    now = 1_700_000_000

    run_id = _claim(conn, task_id, now)
    assert run_id is not None

    row = conn.execute(
        "SELECT started_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["started_at"] == now


def test_reclaim_resets_started_at_to_the_new_run(conn):
    """A re-claim must reset started_at, not preserve the old value.

    This is the regression: with ``COALESCE(started_at, ?)`` the card
    kept its original creation timestamp and instantly looked like it
    had been running for weeks.
    """
    task_id = _make_task(conn, started_at=1_600_000_000)  # ~3 years old
    first_now = 1_700_000_000

    # First claim, then complete and release the card back to ready.
    _claim(conn, task_id, first_now)
    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
        "claim_expires = NULL WHERE id = ?",
        (task_id,),
    )
    conn.commit()

    # Second claim, much later.
    second_now = first_now + 5 * 24 * 3600
    run_id = _claim(conn, task_id, second_now)
    assert run_id is not None

    row = conn.execute(
        "SELECT started_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["started_at"] == second_now, (
        "started_at must track the CURRENT run, not the original creation"
    )


def test_task_started_at_matches_the_run_row(conn):
    """tasks.started_at and the current task_runs.started_at must agree.

    Mission Control reads the task row; the dispatcher reads the run
    row. If they disagree, the two views of "how long has this been
    running" disagree.
    """
    task_id = _make_task(conn, started_at=1_500_000_000)
    now = 1_700_000_000

    run_id = _claim(conn, task_id, now)
    assert run_id is not None

    task_started = conn.execute(
        "SELECT started_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["started_at"]
    run_started = conn.execute(
        "SELECT started_at FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()["started_at"]

    assert task_started == run_started == now


def test_claim_still_transitions_status_and_opens_a_run(conn):
    """The claim semantics themselves are unchanged by the fix."""
    task_id = _make_task(conn)
    now = 1_700_000_000

    run_id = _claim(conn, task_id, now)
    assert run_id is not None

    row = conn.execute(
        "SELECT status, claim_lock, claim_expires, current_run_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert row["status"] == "running"
    assert row["claim_lock"] == "lock-1"
    assert row["claim_expires"] == now + 300
    assert row["current_run_id"] == run_id


def test_claim_lost_cas_does_not_touch_started_at(conn):
    """A failed CAS must not modify the row at all."""
    task_id = _make_task(conn)
    now = 1_700_000_000
    _claim(conn, task_id, now)

    before = conn.execute(
        "SELECT started_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["started_at"]

    # Second claim while the first lock is still held -> CAS loses.
    result = _claim(conn, task_id, now + 10)
    assert result is None

    after = conn.execute(
        "SELECT started_at FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["started_at"]
    assert after == before
