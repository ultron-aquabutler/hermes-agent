---
date: 2026-10-31
agent: Friday
tags: [hermes-agent, kanban, iteration-budget, worker-guidance]
---

# Kanban worker iteration-budget checkpoint

## Overview

Dispatcher-owned Kanban workers receive a one-time progress checkpoint at 75% of their configured iteration budget when no explicit `agent.budget_warning_ratio` is set. The checkpoint asks the worker to prioritize required acceptance criteria, preserve useful workspace progress, and avoid optional follow-ups. It is guidance, not a completion signal or an automatic task split.

The checkpoint does not change `max_iterations` or reduce the hard cap. Required work that is still progressing may continue. An explicit profile warning ratio remains authoritative; non-worker sessions do not gain this Kanban-specific default.

Observed motivation, not a performance claim: two Librarian sessions reached the 500-call limit, while another succeeded after 475 calls. These observations establish that some long runs can approach the cap; they do not establish a specific nonprogress loop, prove token savings, or show that the checkpoint prevents runaway behavior.

## Architecture

`agent/turn_iteration_prep.py::_maybe_inject_iteration_budget_warning` selects the default 0.75 ratio only when all worker-scope checks hold: `HERMES_KANBAN_TASK` is set, the current context is dispatcher-owned, and `kanban_complete` is available. The notice is appended to the newest mutable tool result rather than altering the system prompt or an older persisted message.

The turn preparation path calls the checkpoint before the next request. The tool-progress persistence path also checks it before flushing a tool result, so the model sees the same checkpoint text that is persisted. `agent/prompt_builder.py::KANBAN_GUIDANCE` provides complementary advice about bounded reads, safe alternatives to repeated blocked calls, and splitting only independent remaining work.

## Operation

At 75% of the worker's existing iteration budget, the worker gets one checkpoint notice and continues with the remaining budget. For a 500-iteration budget, the notice begins at 375 used iterations; the hard cap remains 500. The notice is one-shot per turn and attaches to the newest tool result, preserving the stable system prompt and prior transcript rows.

At the checkpoint, compare unfinished work to explicit acceptance criteria. Do not start optional follow-ups. If an oversized remainder is independent, preserve the current workspace and create a linked child task; do not split interdependent criteria or drop required work. If a safe approach is blocked, record completed and pending criteria before escalating. Continue required work that is demonstrably progressing.

Focused verification:

```bash
scripts/run_tests.sh tests/agent/test_iteration_budget_warning.py tests/agent/test_system_prompt.py -k 'test_kanban_checkpoint_is_early_and_does_not_reduce_hard_cap or test_kanban_guidance_documents_progress_budget'
```

## Configuration

No new configuration field is introduced. `agent.budget_warning_ratio` continues to control the checkpoint when explicitly configured. When it is unset, only dispatcher-owned Kanban workers meeting the eligibility checks above receive the default 75% checkpoint. The iteration hard cap remains the existing `max_iterations` value.

## Troubleshooting

- If a dispatcher worker does not receive the default checkpoint, confirm that `HERMES_KANBAN_TASK` is present, the worker is in dispatcher-owned context, and its toolset includes `kanban_complete`.
- If the checkpoint appears at a different ratio, inspect the effective explicit `agent.budget_warning_ratio`; explicit values override the Kanban default.
- If the notice does not appear on an already persisted tool result, do not rewrite that historical row. The checkpoint is only appended to the current mutable tool-result tail.
- The checkpoint is not a runaway-loop detector. These changes do not prove that repeated nonprogress is solved, nor do they guarantee token savings.

## Dependencies

- Hermes Agent turn preparation and tool-progress persistence (`agent/turn_iteration_prep.py`, `agent/tool_executor.py`).
- Dispatcher-owned worker context and Kanban worker tool availability.
- Existing iteration budget and `agent.budget_warning_ratio` configuration behavior.

## See Also

- `agent/prompt_builder.py` — Kanban lifecycle and progress guidance.
- `tests/agent/test_iteration_budget_warning.py` — checkpoint ratio, hard-cap, and worker-scope tests.
- `tests/agent/test_kanban_budget_checkpoint.py` — turn-preparation integration coverage.

## Change History

- 2026-10-31 — Added the 75% Kanban worker checkpoint and bounded progress guidance; code and this document ship together in the `fix/kanban-worker-budget-checkpoint` change.
