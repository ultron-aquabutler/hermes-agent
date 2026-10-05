---
title: "FIX: auto-decompose / auto-specify must not re-spec on a recent block_loop_detected event"
created: 2026-10-05
task: t_832840f6
hub_card: t_2746e801
status: shipped
tags: [kanban, dispatcher, decomposer, specifier, block-loop, fix]
---

## Problem

The auto-decomposer (and the auto-specifier) used to read every triage task and
call the LLM to re-spec it — even when the triage route was caused by
`block_loop_detected` (the unblock/re-block loop detector in
`hermes_cli.kanban_db._route_block`). On `t_81abf0b6` (denisew Syncthing Mac
pairing) this manifested as **3 re-specs in 9 minutes** before the same
decomposer finally produced 4 children that immediately re-blocked for the
same `kind=needs_input` reason. The dispatcher cron kept re-claiming the task
and the decomposer kept re-writing it; the agent doing the work could not
proceed because the underlying human-input blocker (iMac Device ID handoff)
was unchanged.

## Root cause

`BLOCK_RECURRENCE_LIMIT = 2` in `hermes_cli/kanban_db.py:114` correctly detects
the unblock-loop and routes the task to `triage` (emitting
`block_loop_detected`). `block_recurrences` deliberately survives each unblock
— resetting it was the amnesia that let cron-unblock ↔ re-block loops run
unbounded. That part of the design is correct.

The bug is one layer up: **`decompose_task` (and `specify_task`) read triage
tasks and rewrite them without checking whether the trip to triage was caused
by `block_loop_detected`**. The decomposer sees a triage card, calls the aux
LLM, gets a fanout, atomically promotes the children — even when the
underlying human-input blocker is unchanged and the new children will hit the
same gate.

## Fix (shipped in t_832840f6)

A shared guard module — `hermes_cli/kanban_block_loop_guard.py` — is called
BEFORE the LLM round-trip from both `kanban_decompose.decompose_task`
(`hermes_cli/kanban_decompose.py:303+`) and `kanban_specify.specify_task`
(`hermes_cli/kanban_specify.py:190+`).

The guard:

1. Reads `task.block_kind` — if NULL, the triage is not from a block loop
   and the guard is inactive. Organic triage flows through unchanged.
2. Looks up the most recent `block_loop_detected` event for the same
   `block_kind` within the lookback window
   (`kanban.block_loop_lookback_seconds`, default 24h).
3. Checks for a `commented` or `unblocked` event after that loop detection
   — either is the "operator re-engaged" signal that releases the guard.
4. If the loop is active AND the operator has not re-engaged, refuses the
   call. The refusal writes:

   - a comment on the task from `author='auto-decomposer'` (or
     `auto-specifier`) naming the loop event id + lookback context
   - a structured `decompose_refused` (or `specify_refused`) event with
     payload `{reason, block_kind, recurrences, last_block_loop_at, loop_event_id}`
   - ONE throttled `assignee='default'` hub-triage card titled
     `Hub: auto-decompose refused on <task_id> — block-loop active since <ts>`
     per task per 6 hours, so the per-tick cron does not file one every tick

The LLM round-trip is skipped entirely. `DecomposeOutcome` /
`SpecifyOutcome` carries `ok=False, reason='block_loop_active'`.

## Configuration

Three new knobs under `kanban:` in `hermes_cli/config_defaults.py` (all
default ON / 24h):

```yaml
kanban:
  auto_decompose_respect_block_loop: true
  auto_specify_respect_block_loop: true
  block_loop_lookback_seconds: 86400
```

- `auto_decompose_respect_block_loop=False` lets the decomposer LLM proceed
  even with a recent loop — the deliberate override for a long-stale blocked
  card where the operator actually wants a fresh spec.
- `auto_specify_respect_block_loop=False` is the symmetric override for the
  specifier; the two are independent so each surface can be loosened in
  isolation.
- `block_loop_lookback_seconds` widens / narrows the "still blocked today"
  window. 0 disables the guard entirely. A higher value (e.g. 7 days for a
  really stuck card) is preferred over flipping the bool.

A guard internal failure (DB error, schema drift) is **fail-closed**: the
guard refuses rather than letting the LLM call through, because the original
spec-loop bug was caused by exactly that kind of silent skip. The comment
trail records the failure so an operator can investigate.

## Caller paths covered

`maybe_refuse(...)` is the single chokepoint — both
`kanban_decompose.decompose_task` (line 303+ in the new code) and
`kanban_specify.specify_task` (line 190+) call it before their LLM
round-trip. That covers the three reachable paths:

- The dispatcher's `auto_decompose` cron on every tick
- `hermes kanban decompose <id>` / `hermes kanban specify <id>` from the CLI
- The gateway plugin's decompose route

A one-line guard at the dispatcher cron level would have left the manual CLI
and gateway paths open; the function-entry chokepoint is the right shape.

## Tests

`tests/hermes_cli/test_kanban_block_loop_guard.py` (new file, 12 tests):

- `test_decompose_refused_on_recent_block_loop_without_re_engagement` — the
  canonical failure case; aux client is patched but NOT called; root stays
  in triage; `decompose_refused` event recorded.
- `test_decompose_proceeds_when_operator_re_engaged_via_comment` — a
  `commented` event after the loop detection releases the guard.
- `test_decompose_proceeds_when_block_loop_outside_lookback` — old loop
  falls off the window.
- `test_decompose_organic_triage_is_unaffected` — `block_kind` NULL = the
  guard is always inactive.
- `test_specify_*` (three) — symmetric coverage for `specify_task`.
- `test_refusal_throttle_files_only_one_hub_card_per_window` — second
  `surface_refusal` call within the throttle window does NOT file a second
  hub card.
- `test_decompose_knob_off_allows_llm_through_with_active_loop` — operator
  override path.
- `test_guard_check_block_loop_returns_inactive_for_null_block_kind` /
  `test_guard_check_block_loop_kind_mismatch_returns_inactive` /
  `test_guard_surface_refusal_records_comment_and_event_and_hub_card` —
  direct module-API tests of `check_block_loop` and `surface_refusal`.

All 12 pass under `pytest tests/hermes_cli/test_kanban_block_loop_guard.py`;
all 13 existing `test_kanban_decompose*` / `test_kanban_specify*` tests
continue to pass.

## Acceptance criteria status

- [x] `t_81abf0b6` is no longer auto-decomposed or auto-specified. The
      `block_loop_detected` event on 2026-10-05 21:55Z is within the 24h
      lookback; the next dispatch tick's auto-decompose call returns
      `ok=False, reason='block_loop_active'` and surfaces one hub card.
- [x] The 4 children (`t_d0bf5a39`, `t_73f11c89`, `t_c6efc700`,
      `t_1e467f6e`) are NOT re-claimed and re-blocked — they stay held until
      the human-input blocker (Bryan hands back the iMac Device ID) is
      resolved or the hub explicitly re-opens the workstream.
- [x] Unit tests pass under the existing `hermes-agent/venv` test runner.
- [x] This wiki page exists (doc-in-commit protocol, see `SOUL.md`).
- [x] `website/docs/user-guide/features/kanban.md` updated to describe the
      new policy + config knobs.

## Out of scope (deliberately)

- The 4 children already created are not reverted. The hub will let them
  stay `todo` until the iMac handoff arrives; if Bryan wants them deleted
  he'll say so.
- `BLOCK_RECURRENCE_LIMIT` itself — the detection threshold is fine; only
  the propagation was wrong.
- The dispatcher's cron-unblock re-block path — orthogonal.
- `kanban_decompose._SYSTEM_PROMPT` — the LLM was doing what was asked; the
  policy gate is upstream.

## See also

- `hermes_cli/kanban_block_loop_guard.py` — the shared guard module.
- `hermes_cli/kanban_decompose.py:_decompose_refusal_to_outcome` — the
  surface-specific translator.
- `hermes_cli/kanban_specify.py` — symmetric call site.
- `hermes_cli/kanban_db.py:_route_block` — the unblock-loop detector that
  the guard is the propagation partner for.
- `website/docs/user-guide/features/kanban.md` — user-facing docs.