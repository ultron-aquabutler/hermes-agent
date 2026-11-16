"""The card must say WHY a managed worker was lost, not only HOW.

``gateway/session_kanban.record_worker_failure_cause`` already carries the cause the
WORKER observed (``worker_failure_cause: <cause>``) on the attempt's ``worker_result``
event. That event is read by the worker client (``_exit_code``) but NOT by the
dispatcher, which builds the crash text from the worker's LOG file. So the cause
reached the board's event stream and never reached ``last_failure_error`` — the
column triage actually reads.

A card then said only:

    pid 224574 exited with code 1 [managed worker lost: managed_worker_failed]

which names a MECHANISM, not a cause, and sends the next reader hunting for a
mechanism. These tests pin that the cause the worker observed reaches the card's
``last_failure_error``, and that a card with no recorded cause is unchanged.

  A1  a worker that recorded a cause: the cause is in the run error AND in the
      card's ``last_failure_error``.
  A2  a worker that recorded no cause: the booking is byte-for-byte what it was
      before this change (no empty "(cause: )" is invented).
  A3  the cause is the WORKER'S, from the CURRENT run only — a later run's cause
      never leaks backwards into an earlier attempt's booking.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_owner_recovery

from tests.hermes_cli.test_kanban_managed_worker_loss import (
    NOW,
    _absent_pid,
    _Board,
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated HERMES_HOME board plus the owner ledger a receipt points at."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    ledger = tmp_path / "state.db"
    with sqlite3.connect(ledger) as store:
        store.execute(
            "CREATE TABLE session_admissions "
            "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)"
        )
    scratch = _Board(conn, ledger)
    yield scratch
    conn.close()


def _record_cause(board, tid, run_id, cause, output=None):
    """Write the attempt's ``worker_result`` event the way the gateway writes it."""
    board.event(tid, run_id, "worker_result", {
        "pid": _absent_pid(base=3_970_000),
        "claim_lock": board.state(tid)["claim_lock"],
        "exit_code": 1,
        "last_output": output if output is not None else f"worker_failure_cause: {cause}",
    }, NOW + 30)
    board.conn.commit()


# --- A1: the cause the worker observed reaches the card ------------------------------------

def test_the_workers_observed_cause_reaches_the_cards_last_failure_error(board):
    run_id = board.running("t_cause", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    _record_cause(board, "t_cause", run_id, "worker_receipt_timeout")

    board.tick(failure_limit=2)

    run = board.run(run_id)
    row = board.state("t_cause")
    # The run error names the cause, not just the mechanism.
    assert "worker_receipt_timeout" in run["error"]
    assert "managed worker lost: managed_worker_failed" in run["error"]
    # And the card column triage reads carries it too.
    assert row["last_failure_error"], "the card must say something"
    assert "worker_receipt_timeout" in row["last_failure_error"]
    # The cause is also machine-readable in the event payload.
    payload = json.loads(run["metadata"] or "{}")
    assert payload.get("worker_failure_cause") == "worker_receipt_timeout"


def test_the_cause_survives_next_to_the_workers_own_output(board):
    """The worker's own words are kept; the cause is added, not substituted."""
    run_id = board.running("t_both", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    _record_cause(board, "t_both", run_id, "worker_receipt_timeout",
                  output="worker_failure_cause: worker_receipt_timeout\nthe turn was interrupted")

    log = kb.worker_log_path("t_both")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("I pushed the fix and opened the PR.\n", encoding="utf-8")

    board.tick(failure_limit=2)

    run = board.run(run_id)
    assert "worker_receipt_timeout" in run["error"]
    assert "I pushed the fix and opened the PR." in run["error"]


# --- A2: no recorded cause means no change -------------------------------------------------

def test_a_card_with_no_recorded_cause_is_unchanged(board):
    """No cause recorded -> no cause invented. The note stays exactly as it was."""
    run_id = board.running("t_nocause", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    board.tick(failure_limit=2)

    run = board.run(run_id)
    assert "managed worker lost: managed_worker_failed" in run["error"]
    assert "cause:" not in run["error"]
    assert "(cause:" not in run["error"]
    payload = json.loads(run["metadata"] or "{}")
    assert "worker_failure_cause" not in payload


def test_a_worker_result_without_a_cause_marker_is_not_invented(board):
    """A ``worker_result`` carrying ordinary output names no cause."""
    run_id = board.running("t_plain", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    _record_cause(board, "t_plain", run_id, "", output="just some output, no marker")

    board.tick(failure_limit=2)

    run = board.run(run_id)
    assert "(cause:" not in run["error"]
    assert "just some output, no marker" not in run["error"]


# --- A3: run scoping ----------------------------------------------------------------------

def test_a_later_runs_cause_never_leaks_into_an_earlier_attempt(board):
    """The cause is the CURRENT run's evidence. A superseded run's cause is not used."""
    run_id = board.running("t_scope", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    _record_cause(board, "t_scope", run_id, "worker_receipt_timeout")
    # The card moves on: a NEW current run, and the old cause is no longer current.
    board.conn.execute(
        "UPDATE tasks SET current_run_id = 999999 WHERE id = 't_scope'")
    board.conn.commit()

    assert kanban_owner_recovery.worker_failure_cause(board.conn, "t_scope") == ""


def test_the_cause_reader_is_defensive_about_unreadable_payloads(board):
    """A payload that cannot be read is no evidence — never an exception."""
    run_id = board.running("t_broken", claim_expires_in=900, heartbeat_age=30,
                           worker_lost="managed_worker_failed")
    board.conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES ('t_broken', ?, 'worker_result', 'not-json-at-all', ?)", (run_id, NOW + 30))
    board.conn.commit()

    assert kanban_owner_recovery.worker_failure_cause(board.conn, "t_broken") == ""


def test_a_card_with_no_worker_result_reads_no_cause(board):
    board.running("t_none", claim_expires_in=900, heartbeat_age=30,
                  worker_lost="managed_worker_failed")
    board.conn.commit()
    assert kanban_owner_recovery.worker_failure_cause(board.conn, "t_none") == ""
