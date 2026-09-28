"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48






def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True


# ---------------------------------------------------------------------------
# cross_profile_completion rule + board-wide audit (t_15748fe5)
#
# Carve-outs the SQL must NOT false-positive on:
#   1. assignee closes their own task (r.profile == t.assignee) -> no finding
#   2. claimless manual close (r.profile IS NULL) -> no finding
#   3. worker closes via agent tool with HERMES_PROFILE matching assignee -> no finding
# A cross-profile close (e.g. assignee transferred mid-run and hub --force
# closed the old worker's run) MUST surface.
# ---------------------------------------------------------------------------


def _completed_event(run_id, *, ts=None, actor=None, mismatch=False, **extra):
    payload = {"result_len": 0, "summary": None}
    if actor is not None:
        payload["actor"] = actor
    if mismatch:
        payload["actor_mismatch_run_profile"] = True
    payload.update(extra)
    return {
        "kind": "completed",
        "created_at": int(ts if ts is not None else time.time()),
        "run_id": run_id,
        "payload": payload,
    }


def test_cross_profile_completion_silent_when_worker_closes_own_task():
    """Worker closes its own assigned task — the SQL must not surface this."""
    now = int(time.time())
    task = _task(id="t_cp1", assignee="friday", status="done")
    events = [_completed_event(run_id=10, ts=now - 60, actor="friday")]
    runs = [_run(outcome="completed", run_id=10)]
    # Override profile on the run (mirrors the sqlite3.Row shape).
    runs[0]["profile"] = "friday"
    diags = kd.compute_task_diagnostics(task, events, runs, now=now)
    cross = [d for d in diags if d.kind == "cross_profile_completion"]
    assert cross == []


def test_cross_profile_completion_fires_when_run_profile_differs_from_assignee():
    """Hub --force closed an assignee's run while the assignee profile was different."""
    now = int(time.time())
    task = _task(id="t_cp2", assignee="ultron", status="done")
    # run_profile=friday (old worker), assignee=ultron (transferred). Hub --force.
    # The hub claimed ultron (the new assignee) as actor via --as ultron, so
    # actor == assignee but actor != run_profile. That's the
    # "transfer-then-close" case the SQL needs to surface as a warning.
    events = [_completed_event(run_id=11, ts=now - 120, actor="ultron", mismatch=True)]
    runs = [_run(outcome="completed", run_id=11)]
    runs[0]["profile"] = "friday"
    diags = kd.compute_task_diagnostics(task, events, runs, now=now)
    cross = [d for d in diags if d.kind == "cross_profile_completion"]
    assert len(cross) == 1
    d = cross[0]
    assert d.severity == "warning"  # actor==assignee; informational
    assert d.data["assignee"] == "ultron"
    assert d.data["run_profile"] == "friday"
    assert d.data["actor"] == "ultron"
    assert d.data["actor_mismatch_run_profile"] is True
    assert "ultron" in d.title and "friday" in d.title
    # Recovery actions surface
    labels = {a.label for a in d.actions}
    assert any("comment" in lab.lower() for lab in labels)


def test_cross_profile_completion_error_when_third_party_actor():
    """A third party (vision) closed ultron's task via a run that was friday's.
    That's the only anomaly the kernel can detect from this side."""
    now = int(time.time())
    task = _task(id="t_cp2b", assignee="ultron", status="done")
    # actor=vision, run_profile=friday, assignee=ultron — all three differ.
    events = [_completed_event(run_id=11, ts=now - 120, actor="vision", mismatch=True)]
    runs = [_run(outcome="completed", run_id=11)]
    runs[0]["profile"] = "friday"
    diags = kd.compute_task_diagnostics(task, events, runs, now=now)
    cross = [d for d in diags if d.kind == "cross_profile_completion"]
    assert len(cross) == 1
    assert cross[0].severity == "error"
    assert cross[0].data["actor"] == "vision"


def test_cross_profile_completion_warning_when_unattributed():
    """No actor recorded + run profile != assignee -> warning, not error."""
    now = int(time.time())
    task = _task(id="t_cp3", assignee="vision", status="done")
    events = [_completed_event(run_id=12, ts=now - 30, actor=None)]
    runs = [_run(outcome="completed", run_id=12)]
    runs[0]["profile"] = "friday"
    diags = kd.compute_task_diagnostics(task, events, runs, now=now)
    cross = [d for d in diags if d.kind == "cross_profile_completion"]
    assert len(cross) == 1
    assert cross[0].severity == "warning"
    assert cross[0].data["actor"] is None


def test_cross_profile_completion_silent_when_run_profile_null():
    """Claimless manual close (no run row, run_profile IS NULL) — silent."""
    now = int(time.time())
    task = _task(id="t_cp4", assignee="friday", status="done")
    # Event has no run_id (claimless path; _synthesize_ended_run still writes
    # a row, but its profile == assignee, so the rule skips it).
    events = [_completed_event(run_id=13, ts=now - 30, actor="friday")]
    runs = [_run(outcome="completed", run_id=13)]
    runs[0]["profile"] = "friday"  # synthesized run profile == assignee
    diags = kd.compute_task_diagnostics(task, events, runs, now=now)
    cross = [d for d in diags if d.kind == "cross_profile_completion"]
    assert cross == []


def test_board_cross_profile_completions_returns_empty_on_clean_board(kanban_home):
    """The hub pre-measured 0 rows across 5 boards over 30 days. The fresh
    board must match."""
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="normal", assignee="friday")
        rows = kd.board_cross_profile_completions(conn)
        assert rows == []
    finally:
        conn.close()


def test_board_cross_profile_completions_surfaces_cross_profile_close(kanban_home):
    """Round-trip through the real kanban_db: a complete_task call from
    profile A on a task assigned to B surfaces in the audit."""
    from hermes_cli import kanban_db as kb
    conn = kbc.connect()
    try:
        # Create a task assigned to ultron, complete it from friday's POV.
        # Since there's no run row (claimless), the synthesized run's
        # profile = task.assignee = ultron, so the SQL would NOT match —
        # exactly the carve-out behavior. To force a real cross-profile close
        # we'd need to actually have a dispatcher-spawned run; for the
        # audit round-trip we insert an out-of-band task_event + task_runs
        # row that mimics a hub --force close of a worker whose profile !=
        # assignee.
        tid = kb.create_task(conn, title="t", assignee="ultron")
        # Insert a completed event linked to a run whose profile is "friday".
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, ?, 'done', 'completed', ?, ?)",
            (tid, "friday", now, now),
        )
        run_id = int(cur.lastrowid or 0)
        if run_id <= 0:
            pytest.fail("INSERT did not return a row id")
        import json as _json
        payload = _json.dumps({"actor": "friday", "run_profile": "friday",
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
        assert r["actor"] == "friday"
        assert r["actor_mismatch_run_profile"] is True
    finally:
        conn.close()
