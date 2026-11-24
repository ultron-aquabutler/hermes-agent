"""Invariant tests for the no-answer tool-call budget (#137500 family).

The runaway these guard against: a turn that issued a long run of DISTINCT,
SUCCESSFUL probes and never produced an answer. Every pre-existing tool-loop
guardrail needs a REPEAT (identical call, repeating cycle, failing call, or an
idempotent tool with no progress), so none of them could fire — the turn simply
ended as a dead end, and the next compaction re-stated the original request,
restarting the whole task.

Two invariants are pinned here:

1. A turn that spends its tool-call budget WITHOUT an answer gets the
   "stop and write up" directive, and the budget state reports exhaustion.
2. A turn that DID answer is never charged for its probes and never receives
   the directive; the budget resets between turns.
"""

from __future__ import annotations

import pytest

from agent.no_answer_budget import (
    BUDGET_EXEMPT_TOOL_NAMES,
    DEFAULT_NO_ANSWER_TOOL_BUDGET,
    MIN_NO_ANSWER_TOOL_BUDGET,
    NO_ANSWER_BUDGET_REQUEST,
    NoAnswerBudgetState,
    add_no_answer_budget_directive,
    budget_from_config,
)


class _Agent:
    """Minimal agent stand-in: only the attributes the budget touches."""

    def __init__(self, state: NoAnswerBudgetState | None = None):
        self._no_answer_budget = state


def test_budget_binds_on_distinct_successful_calls_and_injects_the_writeup_directive():
    """INVARIANT 1: distinct successful probes must still exhaust the budget.

    This is the exact shape of the runaway: every call is different and every
    call succeeds, so no repeat-based guardrail can fire. The budget binds on
    the COUNT of calls, and the directive reaches the model.
    """
    state = NoAnswerBudgetState(budget=DEFAULT_NO_ANSWER_TOOL_BUDGET)
    agent = _Agent(state)

    # Distinct, successful probes — no identical call, no failure, no cycle.
    budget = DEFAULT_NO_ANSWER_TOOL_BUDGET
    for i in range(budget - 1):
        state.count_call(f"probe_{i}")
        assert not state.exhausted, f"budget bound early at call {i + 1}"

    # The final call brings the count to the budget, which is what binds it.
    state.count_call(f"probe_{budget - 1}")

    assert state.exhausted, "budget must bind once the configured number of calls is spent"
    assert state.remaining == 0

    api_messages = [{"role": "user", "content": "get me status on all profiles board work"},
                    {"role": "tool", "content": "probe output"}]
    suffix = add_no_answer_budget_directive(agent, api_messages)

    assert suffix, "the write-up directive must reach the model"
    assert NO_ANSWER_BUDGET_REQUEST in api_messages[-1]["content"]
    # The directive decorates the OUTGOING copy only: the canonical transcript
    # is untouched, so a compaction of the transcript never sees the nudge.
    assert api_messages[-1]["content"].startswith("probe output")


def test_answered_turn_is_never_charged_and_never_gets_the_directive():
    """INVARIANT 2: a turn that concluded must not be billed for its probes.

    A turn that answered keeps its budget state clean, so the directive cannot
    fire on the NEXT request of the same turn, and the budget resets between
    turns so a long session's later turn does not start already spent.
    """
    state = NoAnswerBudgetState(budget=DEFAULT_NO_ANSWER_TOOL_BUDGET)
    agent = _Agent(state)

    for i in range(DEFAULT_NO_ANSWER_TOOL_BUDGET):
        state.count_call(f"probe_{i}")

    # The model produced its answer: the turn concluded.
    state.mark_answered()

    assert not state.exhausted, "an answered turn must not be budget-exhausted"
    api_messages = [{"role": "tool", "content": "probe output"}]
    assert add_no_answer_budget_directive(agent, api_messages) == ""
    assert "budget" not in api_messages[-1]["content"]

    # The next turn starts fresh: the budget is per TURN.
    state.reset(state.budget)
    assert state.calls == 0
    assert not state.exhausted


def test_bookkeeping_tools_do_not_consume_the_budget():
    """An agent tidying up (todo/memory/kanban) must not be billed for it."""
    state = NoAnswerBudgetState(budget=DEFAULT_NO_ANSWER_TOOL_BUDGET)
    for name in BUDGET_EXEMPT_TOOL_NAMES:
        state.count_call(name)
    assert state.calls == 0
    assert not state.exhausted


def test_config_cannot_silently_lower_the_code_floor_but_can_raise_it():
    """The guarantee is code-owned: a config sync cannot quietly disable it."""
    assert budget_from_config({}) == DEFAULT_NO_ANSWER_TOOL_BUDGET

    # A config that tries to set a tiny budget is clamped to the floor.
    assert budget_from_config({"no_answer_budget": 1}) == MIN_NO_ANSWER_TOOL_BUDGET
    assert budget_from_config({"no_answer_budget": 0}) is None, "0 is the explicit opt-out"

    # A deliberate raise is honoured.
    assert budget_from_config({"no_answer_budget": 500}) == 500


def test_unattended_surface_binds_and_attended_surface_stays_advisory():
    """Nobody is watching a gateway turn, so the bound applies there."""
    assert budget_from_config({}, platform="telegram") == DEFAULT_NO_ANSWER_TOOL_BUDGET
    assert budget_from_config({}, platform="cron") == DEFAULT_NO_ANSWER_TOOL_BUDGET
    # A human at a CLI can say "keep going"; the budget must not end their turn.
    assert budget_from_config({}, platform="cli") is None


def test_raising_the_budget_never_switches_enforcement_on_for_an_attended_surface():
    """A bigger ceiling must not turn an advisory surface into an enforced one.

    Enforcement on an attended surface would cut a human's turn short, which is
    worse than the runaway it prevents. Raising the budget is a request for
    MORE room, not for enforcement to start.
    """
    for platform in ("cli", "tui", "desktop", "acp", "subagent", "api_server"):
        assert budget_from_config({"no_answer_budget": 500}, platform=platform) is None, (
            f"{platform} is attended: the budget must stay advisory"
        )
        assert budget_from_config({"no_answer_budget": 0}, platform=platform) is None
