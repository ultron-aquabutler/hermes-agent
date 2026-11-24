"""Integration tests for the no-answer budget's hard backstop in ``finalize_turn``.

The runaway this guards against ends as a DEAD END: the turn falls out of the
loop after a tool result with no assistant text, the finalizer records
``pending_tool_result``, and the gateway persists the user message and retries.
On compaction the original request is then re-stated, restarting the task.

The backstop changes that ending: an exhausted turn with no answer gets one
extra toolless summary request, so the turn concludes with an answer instead of
silently dead-ending.

Importing ``agent.turn_finalizer`` pulls in ``agent.conversation_loop`` ->
``agent.process_bootstrap`` -> ``hermes_bootstrap``, whose import-time
``_settle_interrupted_update()`` probes ``<repo>/.git/hermes-update-pull``
and whose ``activate_dependencies`` reads ``<home>/manifest.json``. Both
paths land inside the real Hermes home, which ``tests/home_io_guard.py``
refuses — the same pre-existing failure that breaks the repo's own
``test_turn_finalizer_iteration_limit_exit.py``. Stub both seams so the
import side effects stay off the real home and the real git dir.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hermes_cli._early_recovery as _early_recovery
import pm.environments as _pm_environments

# Neutralize the import-time probes BEFORE the finalizer (and therefore
# hermes_bootstrap) is imported. See the module docstring.
_early_recovery.restore_interrupted_pull = lambda *_a, **_kw: False
_pm_environments._payload_manifest = lambda *_a, **_kw: None
# A linked worktree resolves its owner checkout's git dir, which is inside the
# real home. This test does not want dependency borrowing from the owner.
_pm_environments.worktree_owner_root = lambda *_a, **_kw: None

from agent.no_answer_budget import NoAnswerBudgetState
from agent.turn_finalizer import finalize_turn


class _BudgetAgent:
    """Minimal agent stand-in for the finalize_turn path."""

    def __init__(self, *, budget=120, calls=0, answered=False):
        self.max_iterations = 60
        self.iteration_budget = SimpleNamespace(
            remaining=0, used=60, max_total=60
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        self._completion_explainer = False

        self._no_answer_budget = NoAnswerBudgetState(budget=budget)
        self._no_answer_budget.calls = calls
        self._no_answer_budget.answered = answered
        self._handle_no_answer_budget_called = False

    def _handle_no_answer_budget(self, messages, api_call_count):
        self._handle_no_answer_budget_called = True
        return "summary of what the probes found"

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _emit_diagnostic_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _format_turn_completion_explanation(self, _reason):
        return "explanation"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def _finalize(agent, **overrides):
    kwargs = dict(
        final_response=None,
        api_call_count=120,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="pending_tool_result",
    )
    kwargs.update(overrides)
    return finalize_turn(agent, **kwargs)


def test_exhausted_budget_turn_asks_for_a_summary_instead_of_dead_ending(monkeypatch):
    """INVARIANT: an exhausted, unanswered turn must NOT end silently.

    Before the backstop this turn ended on a raw ``tool`` row with
    ``turn_exit_reason == "pending_tool_result"`` and no final response — which
    is what the gateway then retried, forever.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _BudgetAgent(budget=120, calls=120)

    result = _finalize(agent)

    assert agent._handle_no_answer_budget_called is True, (
        "the exhausted turn must get a summary request"
    )
    assert result["final_response"] == "summary of what the probes found"
    assert result["turn_exit_reason"] == "no_answer_budget_exhausted"


def test_answered_turn_is_not_asked_for_a_summary(monkeypatch):
    """A turn that already answered must never be re-prompted for a summary."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _BudgetAgent(budget=120, calls=120, answered=True)

    _finalize(agent, final_response="the real answer")

    assert agent._handle_no_answer_budget_called is False


def test_turn_under_the_budget_is_not_asked_for_a_summary(monkeypatch):
    """A turn that still has budget left is a normal turn — leave it alone."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _BudgetAgent(budget=120, calls=7)

    _finalize(agent)

    assert agent._handle_no_answer_budget_called is False


def test_failed_summary_falls_back_to_the_named_failure_copy(monkeypatch):
    """If even the summary request raises, the user gets a named copy."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _BudgetAgent(budget=120, calls=120)

    def _boom(_messages, _api_call_count):
        raise RuntimeError("provider exploded")

    agent._handle_no_answer_budget = _boom

    result = _finalize(agent)

    assert result["final_response"], "the user must get something, not silence"
    assert "no_answer_budget" in result["final_response"]
    assert "120" in result["final_response"]
