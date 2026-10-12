---
title: "FIX: auto-decompose / auto-specify must not re-spec on a recent block_loop_detected event"
created: 2026-10-05
task: t_832840f6
hub_card: t_2746e801
extends: t_832840f6
extension_task: t_399f6b0a
extension_hub_card: t_503815ed
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

---

## Extension (t_399f6b0a) — Running-card coverage + fanout=false re-check

Shipped in commit `a5929ad89c...` follow-up on branch
`fix/running-card-block-loop-guard-t_399f6b0a` (still on branch, not yet
on `main`).

### Observation

`t_503815ed` (the durable gate card holding the 4 denisew Syncthing
children non-claimable) accumulated 5+ `block_loop_detected` events and
5+ `specified` events in a single hour on 2026-10-12 — the spec-loop
reached a new layer. The original `t_832840f6` spec assumed the spec-loop
only hits `triage` cards; in practice the auto-decomposer was also
re-writing a `running`/`ready` card via the dispatcher cron path.

### Two bugs were conflated

**Bug A — integration gap on the `fanout=false` branch.**

`kanban_decompose.decompose_task` (`hermes_cli/kanban_decompose.py:248`)
calls `kb.specify_triage_task` directly when the LLM replies with
`fanout=false` (single-task spec promotion). That call path emits the
`specified` event WITHOUT going through `kanban_specify.specify_task`,
so the early guard hook at the top of `decompose_task` could refuse but
the fanout=false branch would still emit the `specified` event with the
loop active. On `t_503815ed` the dispatch cron hit this branch every tick.

**Bug B — guard ran AFTER the status gate.**

The original ordering was `_load_triage_task` → `maybe_refuse`. So a
`running`/`ready` card (the status the dispatcher cron was given) returned
`"task is not in triage"` BEFORE the guard got a look at it. The task
body explicitly required `decompose_task` and `specify_task` to return
`block_loop_active` for a running card with active block-loop.

### Fix

**Bug A: re-check inside `_apply_single`.**

`hermes_cli/kanban_decompose.py:_apply_single` now calls
`block_loop_guard.maybe_refuse(surface="decompose", ...)` again
immediately before calling `kb.specify_triage_task`. The re-check uses
`surface="decompose"` so the `auto_decompose_respect_block_loop` knob
owns the decision (an operator who flipped the decompose knob OFF for a
long-stale blocked card does not get the fanout branch re-refused via
the specify knob). The re-check writes a `decompose_refused` event and
the same throttled hub card as the early guard.

**Bug B: reorder both entry points.**

`decompose_task` and `specify_task` now call `maybe_refuse(...)` BEFORE
`_load_triage_task`. The triage-status gate still rejects non-triage
tasks for the LLM round-trip — the early guard exists to give a running
card a clean `block_loop_active` refusal instead of `"task is not in
triage"`. The `running`/`ready`/`triage` distinction does not change
the guard's logic — `check_block_loop` is status-agnostic — but it
means the guard refusal surfaces on the right surface for the caller.

### Tests added

6 new tests in `tests/hermes_cli/test_kanban_block_loop_guard.py`:

- `test_running_card_refused_by_decompose` and
  `test_running_card_refused_by_specify` — acceptance criterion 1.
  Running card with active loop, no follow-up `commented`/`unblocked`,
  both surfaces return `block_loop_active`. Kept in separate functions
  so the decompose-side refusal comment doesn't release the guard for
  the specify-side caller.
- `test_fanout_false_decompose_path_also_refuses` — the bug-A integration
  gap. Loop event is placed at the moment between the early guard and the
  fanout-write step (simulates a fresh `block_loop_detected` landing
  during the LLM round-trip). Outcome `block_loop_active`, no
  `specified` event, `decompose_refused` event recorded.
- `test_fanout_true_decompose_still_refuses_via_early_guard` — symmetric
  guard coverage for the fanout=true path; the early guard refuses
  before any LLM call, no children are created.
- `test_running_card_proceeds_when_operator_unblocked` — `unblocked`
  event after the loop is re-engagement; guard stays inactive even on
  a `running` card. Documents the symmetric release path.
- `test_running_card_guard_active_against_running_status` — the guard
  module itself returns `active=True` for a `running` card with
  `block_kind` set; this is the load-bearing assertion for
  `decompose_task`/`specify_task` to translate it.

### Dispatch-pump integration note

The auto-decompose cron path (`gateway/kanban_watchers_dispatcher.py:
auto_decompose_tick`) calls `decompose_task(tid)` per task in
`list_triage_ids()`. With this fix in place:

- A card transitioning from `running` → `triage` via
  `block_loop_detected` is enumerated by `list_triage_ids()` on the
  next tick. The early guard refuses; no LLM round-trip; no children
  created; one throttled hub card.
- A card staying in `running`/`ready` (e.g. t_503815ed after the
  hub's auto-revert, where the `unblocked` event flipped it back) is
  NOT enumerated by `list_triage_ids()` (status filter) — but if a
  future cron path DOES hand the dispatcher a running task (e.g. a
  per-tick Vision worker calling `decompose_task` directly), the early
  guard refuses it BEFORE the status gate. That is the second layer
  this commit adds.

### Out of scope (unchanged from a5929ad89c)

- No change to `BLOCK_RECURRENCE_LIMIT` (the detection threshold is
  fine).
- No change to `kanban_decompose._SYSTEM_PROMPT` rules.
- No change to the cron-unblock re-block path.
- The 4 denisew children of `t_503815ed` stay held via the structural
  parent-child gate; this card does not unblock them — only the
  operator-merge-to-main + Bryan iMac handoff can do that.