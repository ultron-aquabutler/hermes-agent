"""Bounded ``blocker_auth`` cooldown in the kanban respawn guard (t_498b52ce).

Live incident: on 2026-11-23 the friday gateway's kanban dispatcher spawned 0
workers for ~2h45m (12:49 -> 15:01) while the ready queue stayed non-empty. The
hold-back reason was ``blocker_auth`` on every tick, and the dispatcher's
"consecutive ticks" counter climbed to 36.

Root cause of the *hold*: the main provider (minimax) was unavailable, and the
failure text was auth-flavoured, so the guard matched ``_RESPAWN_AUTH_RE`` and
parked the card. The guard had NO cooldown for the auth family (unlike the
quota / rate-limit families, which both decay), so a provider OUTAGE parked
dispatch indefinitely.

These tests pin the bounded behaviour:

* inside the cooldown -> ``blocker_auth`` (still surfaces for a human);
* after the cooldown -> ``None`` (the next tick re-probes the provider);
* an undated auth stamp (no ended run to anchor against) keeps parking, so a
  genuinely dead credential does not become a hot retry loop;
* ``HERMES_KANBAN_AUTH_BLOCK_SECONDS=0`` disables the cooldown entirely.
"""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _ended_run_id(conn, tid):
    """Claim the task and return its run id (claimed tasks always have one)."""
    kb.claim_task(conn, tid)
    task = kb.get_task(conn, tid)
    assert task is not None and task.current_run_id is not None
    return task.current_run_id


AUTH_ERROR = "HTTP 401 unauthorized: invalid api key from provider"


def _seed_auth_failure(conn, tid, ended_at, error_text=AUTH_ERROR):
    """Leave a task in ``ready`` carrying an auth-flavoured failure stamp."""
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
        (error_text, tid),
    )
    conn.commit()


def test_auth_blocker_parks_within_cooldown(kanban_home, monkeypatch):
    """Inside the auth cooldown the guard still parks with ``blocker_auth``."""
    monkeypatch.setenv("HERMES_KANBAN_AUTH_BLOCK_SECONDS", "1800")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="auth-in-window", assignee="a")
        run_id = _ended_run_id(conn, tid)
        conn.execute(
            "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
            (now, run_id),
        )
        _seed_auth_failure(conn, tid, now)

        monkeypatch.setattr(kbd.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"


def test_auth_blocker_cooldown_elapses_and_allows_reprobe(kanban_home, monkeypatch):
    """After the cooldown the guard returns None so the next tick re-probes.

    This is the t_498b52ce fix: a provider outage must not be able to park
    dispatch forever.
    """
    monkeypatch.setenv("HERMES_KANBAN_AUTH_BLOCK_SECONDS", "1800")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="auth-out-window", assignee="a")
        run_id = _ended_run_id(conn, tid)
        conn.execute(
            "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
            (now, run_id),
        )
        _seed_auth_failure(conn, tid, now)

        # 29 minutes in -> still parked.
        monkeypatch.setattr(kbd.time, "time", lambda: now + 29 * 60)
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"

        # 31 minutes in -> cooldown elapsed, the dispatcher may re-probe.
        monkeypatch.setattr(kbd.time, "time", lambda: now + 31 * 60)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_auth_blocker_without_ended_run_keeps_parking(kanban_home, monkeypatch):
    """An undated auth stamp has nothing to date the cooldown against.

    A legacy row or a hand-written ``last_failure_error`` is indistinguishable
    from a dead credential, so it keeps parking exactly as before this bound
    existed. Returning None here would turn a genuine 401 into a hot loop.
    """
    monkeypatch.setenv("HERMES_KANBAN_AUTH_BLOCK_SECONDS", "1800")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="auth-undated", assignee="a")
        conn.execute(
            "UPDATE tasks SET last_failure_error=? WHERE id=?",
            (AUTH_ERROR, tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"


def test_auth_block_cooldown_disabled_allows_immediate_reprobe(kanban_home, monkeypatch):
    """``HERMES_KANBAN_AUTH_BLOCK_SECONDS=0`` disables the cooldown.

    Matches the quota / rate-limit escape hatches: the card respawns on the
    very next tick instead of entering the parked state.
    """
    monkeypatch.setenv("HERMES_KANBAN_AUTH_BLOCK_SECONDS", "0")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="auth-disabled", assignee="a")
        run_id = _ended_run_id(conn, tid)
        conn.execute(
            "UPDATE task_runs SET outcome='crashed', status='failed', ended_at=? WHERE id=?",
            (now, run_id),
        )
        _seed_auth_failure(conn, tid, now)

        monkeypatch.setattr(kbd.time, "time", lambda: now + 1)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_default_auth_block_seconds_is_bounded():
    """The shipped default must be a bounded cooldown, not 'forever'.

    The historical (lost) implementation used 24 hours; a 24h park of a whole
    ready queue is the failure mode this card is about, so the default is
    asserted to be materially shorter than a day.
    """
    assert 0 < kbd.DEFAULT_AUTH_BLOCK_SECONDS <= 3600
