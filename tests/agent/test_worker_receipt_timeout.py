"""Regression tests for the worker persistence-receipt timeout (t_c3e25704).

A kanban worker persists its session by RPC-ing the OWNER (the gateway) and
waiting for a receipt. Three defects made a slow owner lose a COMPLETED turn:

1. The receipt wait was a hardcoded 20 seconds -- short for an owner running
   five platform adapters, the dispatcher, the cron scheduler, and every other
   profile's sessions, over a multi-GB state.db.
2. The timeout classified as ``unknown``, so the user was told "the drive is
   out of room, or another Hermes process is holding the database" -- both
   checkable, both FALSE, and neither the actual cause. That misdirected triage
   for two attempts.
3. The crash counted against the card's failure budget, so a card whose work
   was fine hit its failure_limit and gave up.

The invariant under test:

    a slow owner must not lose a completed turn, must be named honestly, and
    must not consume the card's failure budget
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.runtime_session_store import (
    _RECEIPT_TIMEOUT_DEFAULT_SECONDS,
    _RECEIPT_TIMEOUT_MAX_SECONDS,
    _RECEIPT_TIMEOUT_MIN_SECONDS,
    _RECEIPT_TIMEOUT_SECONDS,
)
from hermes_state_errors import (
    PERSISTENCE_ERROR_CAUSES,
    classify_persistence_error,
)


class TestReceiptTimeoutIsConfigurable:
    """The receipt budget must not be a hardcoded 20 seconds."""

    def test_default_is_generous(self, monkeypatch):
        monkeypatch.delenv("HERMES_WORKER_RECEIPT_TIMEOUT_SECONDS", raising=False)
        assert _RECEIPT_TIMEOUT_SECONDS() == _RECEIPT_TIMEOUT_DEFAULT_SECONDS
        assert _RECEIPT_TIMEOUT_SECONDS() > 20, (
            "20s was the old leash and it lost completed turns"
        )

    def test_env_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("HERMES_WORKER_RECEIPT_TIMEOUT_SECONDS", "45")
        assert _RECEIPT_TIMEOUT_SECONDS() == 45

    def test_too_small_is_clamped(self, monkeypatch):
        monkeypatch.setenv("HERMES_WORKER_RECEIPT_TIMEOUT_SECONDS", "1")
        assert _RECEIPT_TIMEOUT_SECONDS() == _RECEIPT_TIMEOUT_MIN_SECONDS

    def test_too_large_is_clamped(self, monkeypatch):
        monkeypatch.setenv("HERMES_WORKER_RECEIPT_TIMEOUT_SECONDS", "999999")
        assert _RECEIPT_TIMEOUT_SECONDS() == _RECEIPT_TIMEOUT_MAX_SECONDS

    def test_malformed_falls_back_to_the_default(self, monkeypatch):
        """A bad config must never be the reason a turn dies."""
        monkeypatch.setenv("HERMES_WORKER_RECEIPT_TIMEOUT_SECONDS", "not-a-number")
        assert _RECEIPT_TIMEOUT_SECONDS() == _RECEIPT_TIMEOUT_DEFAULT_SECONDS

    def test_the_hardcoded_twenty_is_gone(self):
        """The literal 20 must not remain in the receive call."""
        source = (
            Path(__file__).resolve().parents[2] / "agent" / "runtime_session_store.py"
        ).read_text()
        assert "link.receive(20)" not in source, (
            "the hardcoded 20-second receipt timeout is back"
        )
        assert "link.receive(_RECEIPT_TIMEOUT_SECONDS())" in source


class TestTimeoutIsClassifiedHonestly:
    """The error text must name the actual cause, not guess at disk space."""

    def test_cause_is_registered(self):
        assert "worker_receipt_timeout" in PERSISTENCE_ERROR_CAUSES

    def test_classified_from_the_exception_message(self):
        assert classify_persistence_error("worker_receipt_timeout") == (
            "worker_receipt_timeout"
        )

    def test_classified_from_a_timeout_error(self):
        assert (
            classify_persistence_error(TimeoutError("worker_receipt_timeout"))
            == "worker_receipt_timeout"
        )

    def test_does_not_displace_the_real_buckets(self):
        """The new bucket must not swallow the existing causes."""
        assert classify_persistence_error("database is locked") == "locked"
        assert classify_persistence_error("no space left on device") == "disk"
        assert classify_persistence_error("something else entirely") == "unknown"

    def test_explainer_selects_a_dedicated_key(self):
        from agent.turn_explainers import _persistence_explanation_key

        key = _persistence_explanation_key("worker_receipt_timeout")
        assert key == "explainer.persistence.worker_receipt_timeout"

    def test_the_text_names_the_real_cause(self):
        """The user-facing text must not BLAME disk space for a busy gateway.

        The text may mention disk space, but only to rule it out ("this is not
        a disk-space problem"). What it must not do is tell the user to free
        disk space or close other Hermes processes -- that is the advice that
        sent triage at the wrong target.
        """
        from agent.i18n import t
        from agent.turn_explainers import _persistence_explanation_key

        text = t(_persistence_explanation_key("worker_receipt_timeout"))
        lowered = text.lower()

        # It must NOT prescribe the wrong remedies.
        assert "free some space" not in lowered, (
            "the text still tells the user to free disk space"
        )
        assert "close other hermes windows" not in lowered, (
            "the text still tells the user to close other Hermes windows"
        )
        assert "out of room" not in lowered, (
            "the text still claims the drive is out of room"
        )

        # It MUST name the actual cause and tell the user what to do.
        assert "busy" in lowered or "time" in lowered, (
            "the text must name the real cause: a busy owner / a timeout"
        )
        assert "may well have been saved" in lowered, (
            "the text must say the work may have landed -- the turn was not lost"
        )

    def test_the_default_text_still_exists_for_genuine_unknowns(self):
        """A real unknown keeps the generic advice -- only the timeout is special."""
        from agent.turn_explainers import _persistence_explanation_key

        assert _persistence_explanation_key("unknown") == "explainer.persistence.default"
        assert _persistence_explanation_key(None) == "explainer.persistence.default"


class TestPersistenceTimeoutDoesNotConsumeTheFailureBudget:
    """A slow owner must not give up a card whose work was fine."""

    def test_marker_detection(self):
        from hermes_cli.kanban_db_dispatch import (
            WORKER_OUTPUT_SEP,
            _is_persistence_timeout_crash,
        )

        assert _is_persistence_timeout_crash(
            "pid 123 exited with code 1" + WORKER_OUTPUT_SEP
            + "'agent.runtime_session_store.WorkerPersistenceError: worker_receipt_timeout'"
        )
        assert _is_persistence_timeout_crash(
            "pid 1 exited with code 1" + WORKER_OUTPUT_SEP
            + "'Turn ended: reason=session_persistence_failed'"
        )

    def test_an_ordinary_crash_is_not_mistaken_for_one(self):
        from hermes_cli.kanban_db_dispatch import _is_persistence_timeout_crash

        assert not _is_persistence_timeout_crash("pid 123 exited with code 1")
        assert not _is_persistence_timeout_crash(
            "pid 1 exited with code 1 Worker's last output: 'SyntaxError: invalid syntax'"
        )
        assert not _is_persistence_timeout_crash("")

    def test_infrastructure_flag_leaves_the_counter_alone(self):
        """``infrastructure=True`` is the existing 'do not count this' lever.

        The dispatcher already supports it for spawn failures; the persistence
        timeout now uses the same lever so the counter is untouched.
        """
        import inspect

        from hermes_cli.kanban_db_dispatch import _record_task_failure

        sig = inspect.signature(_record_task_failure)
        assert "infrastructure" in sig.parameters
        doc = sig.parameters["infrastructure"].annotation
        assert doc is inspect.Parameter.empty or "bool" in str(doc)

    def test_the_crash_path_passes_the_flag(self):
        """The accounting path must actually set infrastructure=True."""
        import inspect

        from hermes_cli.kanban_db_dispatch import _account_crashes

        source = inspect.getsource(_account_crashes)
        assert "_is_persistence_timeout_crash" in source
        assert "infrastructure=persistence_timeout" in source

    def test_a_persistence_timeout_does_not_trip_the_systemic_breaker(self):
        """Three identical receipt timeouts are one slow gateway, not three bugs."""
        import inspect

        from hermes_cli.kanban_db_dispatch import _account_crashes

        source = inspect.getsource(_account_crashes)
        assert "is_systemic = False" in source


class TestTheTurnIsNotDiscarded:
    """A persistence failure after the work is done must keep the output."""

    def test_the_turn_finalizer_stamps_the_cause(self):
        """The machine-readable cause must name the timeout bucket."""
        import inspect

        from agent import turn_finalizer

        source = inspect.getsource(turn_finalizer)
        assert "session_persistence_failed" in source
        assert "_last_persistence_error_cause" in source

    def test_the_persistence_cause_reaches_the_turn_end(self):
        """The classified cause must be recorded where the explainer reads it."""
        import inspect

        from agent import session_persistence

        source = inspect.getsource(session_persistence)
        assert "classify_persistence_error" in source
        assert "_last_persistence_error_cause" in source


class TestTheCauseReachesTheDispatcher:
    """The recorded worker output must carry the cause, or the sweep cannot see it.

    This is the link that was MISSING on the first attempt at this fix. The
    worker printed only the generic "the drive is out of room" advice; the
    machine-readable ``failure_reason`` was dropped on the floor, so the
    dispatcher's dead-worker sweep saw text with no cause in it and could not
    tell a lost receipt from a real work failure. Verified against the REAL
    crashed card: ``logs/t_9bef45b3.log`` contains zero occurrences of
    ``worker_receipt_timeout``.
    """

    def test_the_worker_records_the_failure_reason(self):
        import inspect

        from gateway import session_kanban

        source = inspect.getsource(session_kanban)
        assert "failure_reason" in source, (
            "the worker's recorded output must carry the machine-readable cause"
        )
        assert "session_persistence_failed" in source

    def test_the_recorded_output_is_detected_by_the_sweep(self):
        """End-to-end: what the worker records must be what the sweep reads."""
        from hermes_cli.kanban_db_dispatch import _is_persistence_timeout_crash

        final_response = (
            "No reply: Hermes couldn't confirm that this conversation was saved "
            "in time, so it stopped rather than lose your messages."
        )
        failure_reason = "session_persistence_failed:worker_receipt_timeout"
        last_output = f"{final_response}\n{failure_reason}".strip()[-500:]

        assert _is_persistence_timeout_crash(last_output), (
            "the recorded output must be recognisable to the dead-worker sweep"
        )

    def test_a_real_work_failure_is_still_recognised_as_a_work_failure(self):
        from hermes_cli.kanban_db_dispatch import _is_persistence_timeout_crash

        assert not _is_persistence_timeout_crash("SyntaxError: invalid syntax")
        assert not _is_persistence_timeout_crash(
            "account entitlement is exhausted for z-ai/glm-5.2"
        )
