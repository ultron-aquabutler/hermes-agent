# Hermes No-Answer Tool-Call Budget

A per-turn ceiling on tool calls for a turn that has not yet produced an answer, so a turn
cannot spend hours probing and never conclude.

## Overview

On 2026-11-24 the `default` profile (Vision persona) ran a runaway in the Telegram DM
session `20261110_162723_dc65d8e8`. The originating request was six words —
*"Vision get me status on all profiles board work"* — and it produced **381 messages, 185 tool
calls, and zero final answers** before the gateway restart cut it off.

The loop was not a repetition. Every existing tool-loop guardrail requires a **repeat**:

| Guardrail | Needs | Vision's run |
|---|---|---|
| identical-call guard | the same call twice | every call was distinct |
| repeating-cycle guard | a cycle of calls | no cycle |
| failure guard | a failing call | every call succeeded |
| idempotent no-progress guard | an idempotent tool with no progress | — |

And the no-progress counter was reset by every tool Vision used: `write_file`, `patch`,
`terminal` and `execute_code` are all in `PROGRESS_RESET_TOOL_NAMES`. So a turn of many
**distinct, successful** probes was invisible to every guard, and the turn simply ended as a
dead end.

A dead end is the trigger for the second half of the loop. When the turn ends without
assistant text, the finalizer records `turn_exit_reason = "pending_tool_result"` and the
gateway persists the user message and retries. On compaction, `_find_inflight_user_task`
also re-states the original request, because the transcript no longer ends with a completed
assistant reply. The same request came back at message id `463287` and again at `463332`
with the same `platform_message_id=19566`.

**Fixing the answer ends the loop.** Once the turn produces an answer, the request is no
longer "in flight", the compaction summary stops restating it, and the gateway has nothing
to retry.

### What compaction was and was not doing

Measured from real telemetry, so the diagnosis is not guesswork:

- Compaction ran on a **~41-minute median cadence**, not every 5-10 minutes.
- It reclaimed a lot: average **271,986 tokens before → 72,633 after**, ~119 messages → ~32.
- Post-compaction headroom was **45K-68K tokens** against a 120,000 effective trigger.
- Tool timeouts were **21 of 3,214 results (0.7%)** — not the driver.

So compaction was working. The bug was the turn, not the compactor.

> **Correction to an earlier diagnosis.** An earlier pass attributed this to a
> compaction-truncation feedback loop — a protected tail that re-crossed the 120,000
> threshold within minutes. That was **wrong**, and it was disproven by measurement: the
> compaction cadence was ~41 minutes, not 5-10, and post-compaction headroom was large. The
> real mechanism is the distinct-successful-probe no-answer turn described above.

## Architecture

### The budget

`agent/no_answer_budget.py` holds a per-turn counter:

```python
DEFAULT_NO_ANSWER_TOOL_BUDGET = 120
MIN_NO_ANSWER_TOOL_BUDGET = 40
```

The budget **counts dispatched tool calls per turn**. It is exhausted when the count reaches
the budget and the turn has not yet answered.

| State | Meaning |
|---|---|
| `calls` | tool calls dispatched this turn |
| `answered` | the model produced final assistant text |
| `exhausted` | `calls >= budget` and not `answered` |

`answered` is set in `agent/turn_final_response.py` when the turn concludes with text — so a
turn that *did* answer is never billed for its probes.

The budget **resets every turn** (`agent/turn_context.py`), so a long session's later turn
does not start already spent.

### Three enforcement points

1. **The directive** (`agent/turn_request_assembly.py` →
   `add_no_answer_budget_directive`). Once the budget is exhausted, every subsequent request
   carries a short "stop investigating and write up" instruction appended to the **outgoing
   copy** of the tail message. It decorates only the request payload, never the canonical
   transcript, so a compaction of the transcript never sees the nudge. It is re-asserted on
   every request, so a model that ignores the first notice still cannot keep probing.

2. **The hard backstop** (`agent/turn_finalizer.py`). Before the generic silent-stop
   handling can mint a "send `continue`" message, an exhausted turn with no answer asks the
   model for one extra **toolless** summary request. The turn ends with a real answer instead
   of a dead end. This is the same shape as the existing `max_iterations` summary request,
   factored into a shared `_request_summary` helper.

3. **The failure copy** (`agent/turn_failure_copy.py`). If even the summary request fails,
   the user gets a message naming the limit and what to do next, instead of silence.

### Why it is in CODE and not only in config

Profile configs **do not inherit the root config** — by design. `hermes_cli/config.py` does
`config = deepcopy(DEFAULT_CONFIG)` and deep-merges only the profile's own `config.yaml`.
A `config reset`, a `doctor --fix`, a config migration, or a hand-edited YAML reopens any
config-only hole. The same reasoning that put the compaction ceiling in
`agent/compression_fleet_ceiling.py` applies here.

So the contract is deliberately one-sided:

| Config | Result | Why |
|---|---|---|
| unset | **120** (the code default) | code-owned |
| `40` | **40** | a deliberate raise is honoured |
| `1` | **40** (the floor) | config cannot silently weaken the guard |
| `0` | **disabled** | the explicit opt-out |
| `500` | **500** | a deliberate raise is honoured |

`MIN_NO_ANSWER_TOOL_BUDGET` is the floor. A config sync cannot quietly turn the guard off,
and only an explicit `0` does that.

### Where the guard does not apply

An attended surface — a human at a CLI, TUI, desktop, ACP, subagent or API server — is not
the runaway case, and cutting a human's turn short is worse than the bug. The budget is
**advisory** there: `budget_from_config` returns `None` for those platforms and the guard
never binds. This holds even when the budget is raised above the default — raising the
ceiling must never switch enforcement ON.

Gateway and cron turns are unattended, so the budget binds normally.

## Configuration

```yaml
tool_loop_guardrails:
  no_answer_budget: 120   # per-turn tool-call ceiling; 0 disables; cannot go below 40
```

## Operation

### Verifying the budget is live

```python
from agent.no_answer_budget import (
    DEFAULT_NO_ANSWER_TOOL_BUDGET, budget_from_config,
)

budget_from_config({})                  # -> 120
budget_from_config({"no_answer_budget": 1})   # -> 40  (floor)
budget_from_config({"no_answer_budget": 0})   # -> None (disabled)
budget_from_config({}, platform="cli")  # -> None (attended: advisory)
```

### Reading the guard's log line

```
No-answer tool budget exhausted (120/120) without an answer — requesting summary
```

and in the turn's exit reason:

```
_turn_exit_reason = "no_answer_budget_exhausted"
```

### What a correctly-bounded turn looks like

The turn ends with an assistant message that answers, rather than ending on a raw `tool` row.
In the transcript, the last row of a healthy turn is `assistant` with text; the last row of a
dead-end turn is `tool`.

## Troubleshooting

### A turn was cut off mid-task

That is the guard working. The summary it produced should say what it found and what it did
not. If the task genuinely needs more than 120 tool calls, raise the budget in that profile's
config — it is honoured above the default.

### The guard did not fire on a runaway

Check that the runaway was actually *within one turn*. A runaway that spans many turns —
each answering — is not this guard's case; the per-turn budget resets by design. Look for a
turn with a large tool-call count and no final assistant text.

Also check the platform. On `cli`, `tui`, `desktop`, `acp`, `subagent` and `api_server` the
budget is advisory and never binds.

### A legitimate long investigation hit the budget

Raise it. The budget is per turn, and the default is deliberately generous (120) — the
observed runaway reached 185 tool calls in a single dead-end turn.

### The budget is exhausted but the summary request also failed

The user gets the `no_answer_budget_no_summary` copy: it names the limit and says to send
`continue` or raise `no_answer_budget`. That is the designed last resort, not a new bug.

## Dependencies

- `agent/no_answer_budget.py` — the budget, its state, and the config resolver
- `agent/agent_init.py` — instantiates the per-agent budget state
- `agent/turn_context.py` — resets the budget each turn
- `agent/tool_executor.py` — charges each dispatched tool call
- `agent/turn_request_assembly.py` — injects the write-up directive
- `agent/turn_final_response.py` — marks the turn answered
- `agent/turn_finalizer.py` — the hard backstop
- `agent/chat_completion_helpers.py` — `handle_no_answer_budget` and the shared
  `_request_summary` helper
- `agent/context_compressor.py` — `NO_ANSWER_BUDGET_SUMMARY_REQUEST`
- `agent/turn_failure_copy.py` — the failure copy

## Tests

`tests/agent/test_no_answer_budget.py` — six invariant tests covering the budget binding on
distinct successful probes, an answered turn never being charged, bookkeeping tools being
exempt, the config floor and opt-out, and the attended-surface advisory rule.

## See Also

- `docs/wiki/services/hermes-compaction-fleet-ceiling.md` — the compaction ceiling; the other
  half of the token-spend story
- `docs/wiki/services/hermes-compaction-loop-detector.md` — the usage-independent loop
  detector

Obsidian mirror: `Homelab/10-Services/Services/hermes-no-answer-tool-call-budget.md` — the
vault copy is the canonical published page and carries the same content.
