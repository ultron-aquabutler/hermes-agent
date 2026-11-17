"""A worker whose terminal board call landed must not be booked as a crash.

The worker reaches its verdict (``kanban_complete``/``kanban_block``/
``kanban_request_review``) by writing to the board directly, so the verdict can
land BEFORE the owner loses the worker and settles the admission ``unknown``.
The submitter then read that ``unknown`` as a handshake failure, exited rc=1,
and the dispatcher's sweep booked the attempt as a crash and re-claimed the
card -- a card whose work was done kept looping.

``task_runs.outcome`` is the board's own durable record of the attempt's
verdict, keyed to the run. It is the authority here.
"""
import sqlite3

import pytest

from hermes_cli.kanban_worker_client import _board_terminal_outcome, _exit_code


def _board(tmp_path, outcome, task_id='t_1', run_id=7):
    db = tmp_path / 'kanban.db'
    conn = sqlite3.connect(db)
    conn.execute(
        'CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT)')
    conn.execute('INSERT INTO task_runs (id, task_id, outcome) VALUES (?,?,?)',
                 (run_id, task_id, outcome))
    conn.commit()
    conn.close()
    return db


PARAMS = {'run_id': 7, 'task_id': 't_1', 'claim_lock': 'lock-1'}


def test_unknown_receipt_with_a_blocked_verdict_is_not_a_failure(tmp_path):
    """The board already holds the verdict, so the receipt's 'unknown' loses."""
    db = _board(tmp_path, 'blocked')
    assert _exit_code({'status': 'unknown'}, PARAMS, str(db)) == 0


def test_unknown_receipt_with_a_completed_verdict_is_not_a_failure(tmp_path):
    db = _board(tmp_path, 'completed')
    assert _exit_code({'status': 'unknown'}, PARAMS, str(db)) == 0


def test_unknown_receipt_with_a_review_verdict_is_not_a_failure(tmp_path):
    db = _board(tmp_path, 'review_requested')
    assert _exit_code({'status': 'unknown'}, PARAMS, str(db)) == 0


def test_unknown_receipt_without_a_verdict_still_raises(tmp_path):
    """No verdict on the board: the handshake failure is real."""
    db = _board(tmp_path, 'crashed')
    from hermes_cli.gateway_client import GatewayClientError
    with pytest.raises(GatewayClientError, match='Kanban execution unknown'):
        _exit_code({'status': 'unknown'}, PARAMS, str(db))


def test_a_running_attempt_is_not_a_verdict(tmp_path):
    db = _board(tmp_path, None)
    from hermes_cli.gateway_client import GatewayClientError
    with pytest.raises(GatewayClientError, match='Kanban execution unknown'):
        _exit_code({'status': 'unknown'}, PARAMS, str(db))


def test_another_runs_verdict_does_not_count(tmp_path):
    """The verdict must belong to THIS run, not some earlier attempt."""
    db = tmp_path / 'kanban.db'
    conn = sqlite3.connect(db)
    conn.execute(
        'CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT)')
    conn.execute('INSERT INTO task_runs (id, task_id, outcome) VALUES (99,?,?)',
                 ('t_1', 'completed'))
    conn.commit()
    conn.close()

    from hermes_cli.gateway_client import GatewayClientError
    with pytest.raises(GatewayClientError, match='Kanban execution unknown'):
        _exit_code({'status': 'unknown'}, PARAMS, str(db))


def test_another_cards_verdict_does_not_count(tmp_path):
    """A verdict on a different card of the same run id must not count."""
    db = tmp_path / 'kanban.db'
    conn = sqlite3.connect(db)
    conn.execute(
        'CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT)')
    conn.execute('INSERT INTO task_runs (id, task_id, outcome) VALUES (7,?,?)',
                 ('t_other', 'completed'))
    conn.commit()
    conn.close()

    from hermes_cli.gateway_client import GatewayClientError
    with pytest.raises(GatewayClientError, match='Kanban execution unknown'):
        _exit_code({'status': 'unknown'}, PARAMS, str(db))


def test_terminal_receipt_still_reads_the_worker_result(tmp_path):
    """The normal terminal path is unchanged: it reads the attempt's exit code."""
    db = tmp_path / 'kanban.db'
    conn = sqlite3.connect(db)
    conn.execute(
        'CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT)')
    conn.execute('INSERT INTO task_runs (id, task_id, outcome) VALUES (7,?,?)',
                 ('t_1', 'completed'))
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, "
        "kind TEXT, payload TEXT)")
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?,?,?,?)",
                 ('t_1', 7, 'worker_result',
                  '{"claim_lock": "lock-1", "exit_code": 0, "last_output": "done"}'))
    conn.commit()
    conn.close()

    assert _exit_code({'status': 'terminal'}, PARAMS, str(db)) == 0


def test_missing_board_is_not_a_verdict(tmp_path):
    """An unreadable board must not be mistaken for a success."""
    from hermes_cli.gateway_client import GatewayClientError
    with pytest.raises(GatewayClientError, match='Kanban execution unknown'):
        _exit_code({'status': 'unknown'}, PARAMS, str(tmp_path / 'nope.db'))


def test_board_terminal_outcome_returns_none_for_a_non_terminal_run(tmp_path):
    db = _board(tmp_path, 'crashed')
    assert _board_terminal_outcome(str(db), PARAMS) is None
