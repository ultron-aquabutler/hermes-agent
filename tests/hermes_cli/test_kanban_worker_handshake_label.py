"""A handshake failure must never be reported as an owner refusal (t_bdfd455e).

``GatewayClientError`` subclasses ``ValueError``, so ``_submit``'s broad
``except (KeyError, ValueError, OSError)`` used to swallow the handshake failure and
print "Kanban owner refused: Kanban execution unknown; retained by owner". Nothing was
refused: the owner admitted the card and the failure came afterwards, in the completion
handshake. The label asserted a cause that was false, and it is the text the next reader
sees in ``task_runs.error``, the dispatcher's crash summary and ``last_failure_error``.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_worker_client as kwc
from hermes_cli.gateway_client import GatewayClientError


@pytest.fixture
def claimed_env(monkeypatch):
    """``_submit`` reads the claim's env before it calls ``run``; without it every
    failure is a missing-env KeyError, which is a different path entirely.

    ``_submit`` also POPS every ``HERMES_KANBAN_*`` var before handing over to the owner,
    so the fixture restores them for each test that needs them.
    """
    def _set():
        monkeypatch.setenv('HERMES_KANBAN_BOARD', 'homelab')
        monkeypatch.setenv('HERMES_KANBAN_TASK', 't_test')
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '1')
        monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', 'lock')
        monkeypatch.setenv('HERMES_KANBAN_DB', '/tmp/nonexistent-kanban.db')
    _set()
    return _set


def test_gateway_client_error_is_a_value_error():
    """The premise of the bug: the broad handler caught the handshake failure."""
    assert issubclass(GatewayClientError, ValueError)


def test_a_handshake_failure_is_not_reported_as_an_owner_refusal(monkeypatch, capsys, claimed_env):
    """The handshake failure prints its own cause and never the refusal label."""
    def _raise(*_args, **_kwargs):
        raise GatewayClientError('Kanban execution unknown; retained by owner')

    monkeypatch.setattr(kwc, 'run', _raise)
    claimed_env()
    rc = kwc._submit()
    err = capsys.readouterr().err
    assert rc == 1
    assert 'Kanban owner refused' not in err
    assert 'Kanban handshake failed' in err
    assert 'Kanban execution unknown; retained by owner' in err


def test_an_owner_that_never_became_ready_says_owner_unavailable(monkeypatch, capsys, claimed_env):
    """The owner-never-ready case is not a refusal and not a handshake failure either.

    This is the "Kanban owner silent for 182s: Gateway draining: not_ready" path
    (kanban_worker_client.py:106). Nothing was admitted -- the owner never became
    ready -- so "handshake failed" would misdescribe it as a post-admission crash.
    """
    def _raise(*_args, **_kwargs):
        raise GatewayClientError('Kanban owner silent for 182s: Gateway draining: not_ready')

    monkeypatch.setattr(kwc, 'run', _raise)
    claimed_env()
    rc = kwc._submit()
    err = capsys.readouterr().err
    assert rc == 1
    assert 'Kanban owner refused' not in err
    assert 'Kanban owner unavailable' in err
    assert 'Kanban owner silent for 182s' in err


def test_a_real_env_or_owner_refusal_still_says_owner_refused(monkeypatch, capsys, claimed_env):
    """The genuine refusal keeps its label: only the handshake path was mislabelled."""
    def _raise(*_args, **_kwargs):
        raise KeyError('HERMES_KANBAN_BOARD')

    monkeypatch.setattr(kwc, 'run', _raise)
    claimed_env()
    rc = kwc._submit()
    err = capsys.readouterr().err
    assert rc == 1
    assert 'Kanban owner refused' in err
    assert 'Kanban handshake failed' not in err


def test_a_plain_value_error_still_says_owner_refused(monkeypatch, capsys, claimed_env):
    """A non-gateway ValueError keeps the old label; only the gateway one is split out."""
    def _raise(*_args, **_kwargs):
        raise ValueError('not a gateway problem')

    monkeypatch.setattr(kwc, 'run', _raise)
    claimed_env()
    rc = kwc._submit()
    err = capsys.readouterr().err
    assert rc == 1
    assert 'Kanban owner refused' in err
    assert 'Kanban handshake failed' not in err


def test_the_exit_trailer_is_still_written_on_a_handshake_failure(monkeypatch, capsys, claimed_env):
    """The dead-worker sweep books the exit from the trailer; it must still be there."""
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

    def _raise(*_args, **_kwargs):
        raise GatewayClientError('Kanban execution unknown; retained by owner')

    monkeypatch.setattr(kwc, 'run', _raise)
    claimed_env()
    rc = kwc.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert f'{KANBAN_WORKER_EXIT_TRAILER}1' in err
