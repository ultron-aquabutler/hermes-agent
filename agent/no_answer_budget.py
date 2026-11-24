"""Per-turn "no answer yet" tool-call budget: the guard for a loop that never concludes.

Every existing tool-loop guardrail needs a *repeat* to fire: an identical
(tool, args, result) call, a repeating multi-call cycle, a failing call, or an
idempotent tool that stops producing progress. A model that instead issues a
long run of DISTINCT, SUCCESSFUL probes and never concludes evades all of them
— every call returns fresh data, so nothing looks like a stall.

That is the Vision/default-profile failure (#137500 family): a six-word request
produced 381 messages and 185 tool calls across four compactions, every turn a
dead end (zero final assistant text), and the agent promised "then write up"
six times without ever writing up.

This module is the missing guard. It counts the tool calls of ONE turn and, once
the budget is spent, ends the turn by asking the model to summarize what it has
— instead of letting it keep probing. The budget is per turn, so a turn that
does answer is never charged for the previous turn's work.

The budget is a CODE-level default (``DEFAULT_NO_ANSWER_TOOL_BUDGET``) for the
same reason ``agent/compression_fleet_ceiling.py`` owns its ceiling: a config
value alone can be overwritten by a later deploy or a config sync, so the
guarantee has to live in the code. A config key may RAISE the budget (a
deliberately long audit) but never silently lowers it below the floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: Tool calls allowed in one turn before the turn is asked to conclude.
#: Sized above the longest legitimate "gather then report" turn observed
#: (Vision's healthy predecessor session answered after 147 tool calls) and
#: well below the runaway's 185 calls spread over zero answers.
DEFAULT_NO_ANSWER_TOOL_BUDGET = 120

#: Never drop below this: a turn that legitimately needs many probes (a fleet
#: audit, a migration sweep) must still be able to finish on its own terms.
MIN_NO_ANSWER_TOOL_BUDGET = 40

#: Tool calls whose budget does NOT count toward the budget: they are the
#: agent's own bookkeeping (todo lists, memory writes) rather than new
#: information. Counting them would punish an agent for tidying up.
BUDGET_EXEMPT_TOOL_NAMES = frozenset({
    "todo", "todo_list", "memory", "skill_manage", "skill_view", "kanban_comment",
    "kanban_heartbeat", "kanban_create", "kanban_complete", "kanban_block",
})

#: The nudge sent to the model when the budget is spent. It names the budget so
#: the model can see the bound, and asks for the answer it is sitting on.
NO_ANSWER_BUDGET_REQUEST = (
    "You have used this turn's tool-call budget without delivering an answer. "
    "Stop investigating and write up your findings now: what you found, what you "
    "did not find, and what the user should do next. Do not call any more tools."
)

#: Config section + key. ``tool_loop_guardrails.no_answer_budget`` raises the
#: per-turn budget above the code floor; ``0`` disables the guard entirely
#: (opt-out, for a deliberate open-ended investigation).
NO_ANSWER_BUDGET_CONFIG_KEY = "no_answer_budget"

#: Platforms where a human is watching and may want the agent to keep digging.
#: The unattended gateway/cron surfaces are the ones that need the hard bound:
#: nobody is there to say "enough".
ATTENDED_PLATFORMS = frozenset({"cli", "tui", "desktop", "acp", "subagent", "api_server"})


def _as_int(value: Any, default: int) -> int:
    """Best-effort int from a config value; ``default`` on anything unusable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def budget_from_config(data: Any, *, platform: Optional[str] = None) -> Optional[int]:
    """The per-turn no-answer tool budget for this config/platform.

    ``None`` means the guard is off. A configured value may raise the budget
    above the code default but is clamped to ``MIN_NO_ANSWER_TOOL_BUDGET`` so a
    config sync cannot quietly disable the guarantee; ``0`` is the explicit
    opt-out.

    On an ATTENDED platform (a human watching) the guard stays advisory: it
    warns but never ends the turn, because a person can say "keep going" and
    the cost of that is bounded by them. On an unattended surface the guard is
    the only thing that stops a runaway, so it binds there.
    """
    raw = data
    if isinstance(data, Mapping):
        raw = data.get(NO_ANSWER_BUDGET_CONFIG_KEY, data.get("no_answer_tool_budget"))
    if raw is None:
        budget: Optional[int] = DEFAULT_NO_ANSWER_TOOL_BUDGET
    else:
        budget = _as_int(raw, DEFAULT_NO_ANSWER_TOOL_BUDGET)
    if budget <= 0:
        return None
    budget = max(budget, MIN_NO_ANSWER_TOOL_BUDGET)
    if isinstance(platform, str) and platform.strip().lower() in ATTENDED_PLATFORMS:
        # Attended: advisory only. Reported, never enforced. A human can say "keep
        # going", and the cost of that overrun is bounded by them. This holds even
        # when the budget is raised above the default — raising the ceiling must
        # never switch enforcement ON for a surface where it was OFF.
        return None
    return budget


@dataclass
class NoAnswerBudgetState:
    """Per-turn counter for the no-answer budget."""

    budget: Optional[int] = None
    calls: int = 0
    answered: bool = False
    warned: bool = False

    def reset(self, budget: Optional[int]) -> None:
        self.budget = budget
        self.calls = 0
        self.answered = False
        self.warned = False

    def count_call(self, tool_name: str) -> None:
        """Record one tool call; exempt bookkeeping tools do not count."""
        if tool_name in BUDGET_EXEMPT_TOOL_NAMES:
            return
        self.calls += 1

    def mark_answered(self) -> None:
        """The model produced final assistant text: the turn concluded."""
        self.answered = True

    @property
    def exhausted(self) -> bool:
        """True once the budget is spent and the turn still has no answer."""
        return (
            self.budget is not None
            and self.calls >= self.budget
            and not self.answered
        )

    @property
    def remaining(self) -> Optional[int]:
        """Calls left before the budget binds (``None`` when the guard is off)."""
        if self.budget is None:
            return None
        return max(0, self.budget - self.calls)


def budget_state(agent: Any) -> Optional[NoAnswerBudgetState]:
    """The agent's no-answer budget state, or ``None`` when unavailable."""
    return getattr(agent, "_no_answer_budget", None)


def add_no_answer_budget_directive(
    agent: Any, api_messages: list[dict[str, Any]]
) -> str:
    """Append the "stop and write up" directive to the outgoing tail.

    Mirrors ``add_empty_response_retry_hint``: the directive decorates only the
    outgoing copy of the last user/tool row, so the canonical transcript is
    untouched and the directive disappears with the request.

    The directive is re-asserted on every request while the budget is spent, so
    a model that ignores the first notice still cannot keep probing. It is
    appended to the tail rather than sent as a fresh user turn on purpose: a
    fresh user turn would be indistinguishable from a real user message, and
    the compaction layer treats such rows as task restarts.
    """
    state = budget_state(agent)
    if state is None or not state.exhausted:
        return ""
    if not api_messages:
        return ""
    tail = api_messages[-1]
    if tail.get("role") not in {"user", "tool"}:
        return ""
    suffix = "\n\n" + NO_ANSWER_BUDGET_REQUEST
    content = tail.get("content")
    if isinstance(content, list):
        content = [*content, {"type": "text", "text": suffix}]
    else:
        content = (content or "") + suffix
    api_messages[-1] = {**tail, "content": content}
    return suffix
