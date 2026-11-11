"""A worker that dies inside its turn must leave the card a CAUSE, not a blank (t_bdfd455e).

The managed worker's turn recorder never runs when the worker itself dies, so the attempt's
``worker_result`` carried an empty ``last_output`` and the board could not tell a lost
persistence receipt from a failed task. The cause is known only inside the worker, so it
travels with the worker: it is stamped onto the attempt's ``worker_result`` as a new event
(the reader takes the latest), and the card's recorded output names the cause.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

# ``agent.managed_worker`` imports ``hermes_bootstrap`` at module scope, and the bootstrap's
# dependency activation reads ``<checkout-parent>/manifest.json`` -- which, for a checkout that
# lives inside the Hermes home, is a file in the REAL home. The home I/O guard is installed by
# the autouse ``_forbid_real_hermes_home_io`` fixture, which is function-scoped: a MODULE-scope
# import happens at collection time, before the guard exists, and passes. Importing inside a
# test body trips the guard on that manifest read.
#
# So the import belongs here, at module scope, like every other test of this module
# (test_managed_worker_tool_events.py, test_managed_worker_result_usage.py).
from agent.managed_worker import _failure_cause
from agent.runtime_session_store import WorkerPersistenceError


def _isolate_home(tmp_path, monkeypatch):
    """Point every home-derived path at the sandbox BEFORE the kanban modules resolve one."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A board with one claimed card and a bound worker for that exact attempt."""
    _isolate_home(tmp_path, monkeypatch)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    kb.create_board('owned')
    with closing(connect(board='owned')) as conn:
        task_id = kb.create_task(conn, title='Cause carrier', body='Goal', assignee='default',
                                 workspace_kind='scratch')
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, task_id)
        db = str(Path(conn.execute('PRAGMA database_list').fetchall()[0][2]))
    return {'db': db, 'task_id': task.id, 'run_id': task.current_run_id,
            'claim_lock': task.claim_lock}


def _worker_result_payload(db, task_id, run_id):
    with closing(sqlite3.connect(db)) as conn:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result' "
            "ORDER BY id DESC LIMIT 1", (task_id, run_id)).fetchone()
    return json.loads(row[0]) if row else {}


@pytest.fixture(autouse=True)
def bound_worker(monkeypatch):
    """The board only accepts a cause from the interpreter bound to THIS attempt."""
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, '_bound_worker_matches', lambda conn, context: True)


def test_a_failure_cause_reaches_the_cards_recorded_output(board):
    """The cause lands on the attempt's worker_result, so the board shows WHY."""
    from gateway.session_kanban import record_worker_failure_cause

    record_worker_failure_cause(board, 'worker_receipt_timeout')
    payload = _worker_result_payload(board['db'], board['task_id'], board['run_id'])
    assert 'worker_receipt_timeout' in payload.get('last_output', '')


def test_the_cause_is_recorded_even_when_the_attempt_had_no_output(board):
    """The blank case is the one that hid the failure: the cause still lands."""
    from gateway.session_kanban import record_worker_failure_cause

    record_worker_failure_cause(board, 'worker_receipt_timeout')
    payload = _worker_result_payload(board['db'], board['task_id'], board['run_id'])
    assert payload.get('last_output', '').strip()
    assert 'worker_failure_cause: worker_receipt_timeout' in payload['last_output']


def test_an_existing_output_is_kept_alongside_the_cause(board):
    """The worker's own words survive: the cause is appended, not substituted."""
    from gateway.session_kanban import _record_worker_result, record_worker_failure_cause

    _record_worker_result(board, 1, 'the work actually finished')
    record_worker_failure_cause(board, 'worker_receipt_timeout')
    payload = _worker_result_payload(board['db'], board['task_id'], board['run_id'])
    assert 'the work actually finished' in payload['last_output']
    assert 'worker_receipt_timeout' in payload['last_output']
    assert payload.get('exit_code') == 1


def test_the_cause_is_recorded_once(board):
    """A repeated record does not pile the same stamp up."""
    from gateway.session_kanban import record_worker_failure_cause

    record_worker_failure_cause(board, 'worker_receipt_timeout')
    record_worker_failure_cause(board, 'worker_receipt_timeout')
    payload = _worker_result_payload(board['db'], board['task_id'], board['run_id'])
    assert payload['last_output'].count('worker_failure_cause: worker_receipt_timeout') == 1


def test_no_cause_is_never_recorded(board):
    """An empty cause writes nothing: the board must not gain a blank event."""
    from gateway.session_kanban import record_worker_failure_cause

    record_worker_failure_cause(board, '')
    with closing(sqlite3.connect(board['db'])) as conn:
        count = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result'",
            (board['task_id'], board['run_id'])).fetchone()[0]
    assert count == 0


def test_a_cause_from_a_foreign_claim_is_dropped(board, monkeypatch):
    """Only the bound interpreter for this attempt may write the cause."""
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, '_bound_worker_matches', lambda conn, context: False)
    session_kanban.record_worker_failure_cause(board, 'worker_receipt_timeout')
    payload = _worker_result_payload(board['db'], board['task_id'], board['run_id'])
    assert payload == {}


def test_the_failure_cause_names_the_workers_own_observation():
    """The cause is read from the exception or the store's sticky failure -- never a
    traceback (a traceback may carry credentials)."""
    assert _failure_cause(WorkerPersistenceError('worker_receipt_timeout')) == 'worker_receipt_timeout'

    store = type('Store', (), {'failure': 'outbox_full'})()
    assert _failure_cause(ValueError('unrelated'), store) == 'outbox_full'

    # A plain exception with no store: named by type only, message never sent.
    cause = _failure_cause(ValueError('sk-secret-abc123'), None)
    assert cause == 'ValueError'
    assert 'sk-secret-abc123' not in cause
