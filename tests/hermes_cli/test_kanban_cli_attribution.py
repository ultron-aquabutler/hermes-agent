"""Tests for kanban-cli-attribution (t_15748fe5).

Covers:
  - ``complete_task`` records the caller-attested actor on the
    ``completed`` event payload, plus ``run_profile`` for auditability.
  - The ``--as`` CLI flag overrides ``$HERMES_PROFILE`` env; explicit
    empty string is the "no actor" choice (recorded as ``actor=None``).
  - The board-wide ``cross-profile-closes`` subcommand runs the same
    SQL the hub pre-verified, via the public handler.

The hard invariant — *unattributed close is recorded as explicitly
unattributed, not silently blank* — is checked here so a regression to
"actor missing entirely" fails loudly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _latest_completed_event(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT payload, run_id FROM task_events "
        "WHERE task_id = ? AND kind = 'completed' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None, f"no completed event for {task_id}"
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload) or {}
    assert isinstance(payload, dict)
    return {"payload": payload, "run_id": row["run_id"]}


def test_complete_task_records_actor_on_event_payload(conn):
    """Caller-attested actor lands on the completed event payload."""
    tid = kb.create_task(conn, title="actor test", assignee="friday")
    ok = kb.complete_task(conn, tid, result="ok", actor="friday")
    assert ok is True
    ev = _latest_completed_event(conn, tid)
    assert ev["payload"]["actor"] == "friday"
    # run_profile reflects the synthesized row's profile (== assignee for claimless).
    assert ev["payload"]["run_profile"] == "friday"


def test_complete_task_unattributed_records_actor_none(conn):
    """actor=None must be recorded explicitly, not silently absent."""
    tid = kb.create_task(conn, title="unattributed", assignee="friday")
    ok = kb.complete_task(conn, tid, result="ok", actor=None)
    assert ok is True
    ev = _latest_completed_event(conn, tid)
    assert "actor" in ev["payload"]
    assert ev["payload"]["actor"] is None
    # run_profile still present so the audit query is meaningful.
    assert ev["payload"]["run_profile"] == "friday"


def test_complete_task_strips_blank_actor_to_none(conn):
    """Whitespace-only actor becomes None — no surprise null bytes."""
    tid = kb.create_task(conn, title="blank actor", assignee="friday")
    ok = kb.complete_task(conn, tid, result="ok", actor="   ")
    assert ok is True
    ev = _latest_completed_event(conn, tid)
    assert ev["payload"]["actor"] is None


def test_complete_task_mismatch_flag_only_when_actor_and_run_profile_differ(conn):
    """A worker closing its own assigned task shows no mismatch flag."""
    # Claimless manual close: run_profile == assignee. No mismatch.
    tid = kb.create_task(conn, title="self close", assignee="friday")
    kb.complete_task(conn, tid, result="ok", actor="friday")
    ev = _latest_completed_event(conn, tid)
    assert ev["payload"].get("actor_mismatch_run_profile") is None or \
        ev["payload"].get("actor_mismatch_run_profile") is False


def test_complete_task_actor_round_trips_through_board_audit(conn):
    """End-to-end: complete_task -> task_event row -> board audit reads it back."""
    # Insert a hub-style cross-profile close out-of-band (claimless path
    # would synthesize a run with profile=assignee and never surface, so
    # we manufacture the cross-profile scenario directly).
    tid = kb.create_task(conn, title="cross", assignee="ultron")
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        "VALUES (?, ?, 'done', 'completed', ?, ?)",
        (tid, "friday", now, now),
    )
    run_id = int(cur.lastrowid or 0)
    assert run_id > 0
    payload = json.dumps({"actor": "ultron", "run_profile": "friday",
                          "actor_mismatch_run_profile": True,
                          "result_len": 0, "summary": "hub --force"})
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, run_id, created_at) "
        "VALUES (?, 'completed', ?, ?, ?)",
        (tid, payload, run_id, now),
    )
    conn.commit()
    rows = kd.board_cross_profile_completions(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["task_id"] == tid
    assert r["assignee"] == "ultron"
    assert r["run_profile"] == "friday"
    assert r["actor"] == "ultron"
    assert r["actor_mismatch_run_profile"] is True


def test_complete_cli_as_flag_overrides_env(monkeypatch, tmp_path):
    """End-to-end via CLI: --as friday overrides HERMES_PROFILE=ultron."""
    import argparse
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban import _cmd_complete

    # Set up an isolated board.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "ultron")  # would otherwise be used
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cli", assignee="friday")

    args = argparse.Namespace(
        task_ids=[tid], result="ok", summary=None, metadata=None,
        actor="friday", force=False,
    )
    rc = _cmd_complete(args)
    assert rc == 0, "CLI complete should succeed"

    with kbc.connect() as conn:
        ev = _latest_completed_event(conn, tid)
    assert ev["payload"]["actor"] == "friday"


def test_complete_cli_no_as_falls_back_to_hermes_profile(monkeypatch, tmp_path):
    """No --as and HERMES_PROFILE set -> actor mirrors the env."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban import _cmd_complete
    import argparse

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "ultron")
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cli", assignee="friday")

    args = argparse.Namespace(
        task_ids=[tid], result="ok", summary=None, metadata=None,
        actor=None, force=False,
    )
    rc = _cmd_complete(args)
    assert rc == 0
    with kbc.connect() as conn:
        ev = _latest_completed_event(conn, tid)
    assert ev["payload"]["actor"] == "ultron"


def test_complete_cli_explicit_empty_as_records_unattributed(monkeypatch, tmp_path):
    """``--as ""`` is the explicit 'no actor' choice — recorded as actor=None."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban import _cmd_complete
    import argparse

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_PROFILE", "ultron")  # would otherwise be used
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cli", assignee="friday")

    args = argparse.Namespace(
        task_ids=[tid], result="ok", summary=None, metadata=None,
        actor="", force=False,  # explicit empty -> no env fallback
    )
    rc = _cmd_complete(args)
    assert rc == 0
    with kbc.connect() as conn:
        ev = _latest_completed_event(conn, tid)
    # actor is recorded as None even though HERMES_PROFILE was set, because
    # the operator explicitly opted out.
    assert "actor" in ev["payload"]
    assert ev["payload"]["actor"] is None
