# Kanban Block-Loop Quarantine (hub_escalation gate)

> Wiki page for t_8b48a01f (2026-08-28) + t_efc7769a (2026-09-23
> restoration after the branch-drift regression).
>
> **Status:** restored on branch
> `fix/kanban-human-gate-and-quarantine-restoration` (off the deployed
> branch `fix/kanban-recompute-ready-blocker-gate @ d437932a74`).
> Acceptance criteria pass on the deployed head: 73 passed, 1 skipped
> across `test_kanban_db*` / `test_kanban_decompose` /
> `test_kanban_specify` / `test_kanban_block_kinds`.
>
> **Doc-in-commit gap:** the Obsidian vault write path on this host has
> been returning 201 but not persisting since 2026-09-18 (LXC in-process
> cache, see t_f1551b54). This stage-file in the worktree is the source
> of truth; the wiki page will be re-issued when the vault cache clears.
> Kanban comment on `t_efc7769a` flags the gap.

## Overview

The Kanban loop breaker has two complementary guards, both locked on
2026-08-28 and verified live against `t_08b0d2a5` / `t_ff548cb7`:

1. **`block_task` sets `hub_escalation=1` at the unblock-loop trip.**
   When a task trips `BLOCK_RECURRENCE_LIMIT` (default 2), the same
   `UPDATE tasks SET status='triage', hub_escalation=1` writes both —
   quarantine is **atomic** with the loop-detected transition, not a
   later hook.
2. **The auto-decomposer + auto-specify paths refuse to act on
   `hub_escalation=1` rows.** `list_triage_ids()` (both modules) drops
   them, so the dispatcher-side sweep never even sees them. Direct CLI
   invocations of `hermes kanban decompose` / `hermes kanban specify`
   on a quarantined card are gated with a logged refusal + a
   `decomposer_skipped reason=quarantined` audit event. The auxiliary
   LLM is **never** called.

Combined, the two guards break the structural loop

```
block_loop_detected → triage
  → auto-specify → ready → worker → block
  → unblock → re-block (trip) → triage → …
```

which was firing on the deploy once every ~10s once `block_recurrences`
hit the limit.

## Architecture

### Decision ownership

The choice of `hub_escalation` (over a separate `tasks.quarantined`
column) means the same flag serves two roles:

| Role | Set by | Read by |
|---|---|---|
| Relay hub-routing | `kanban_board_watcher.get_hub_escalation` | gateway dispatcher |
| Auto-decomposer refusal | `_route_block` in `hermes_cli/kanban_db.py` | `kanban_decompose.list_triage_ids`, `kanban_specify.list_triage_ids`, `kanban_decompose.decompose_task`, `kanban_specify.specify_task` |

The flip happens in one place (`_route_block`), so future changes
("what triggers a quarantine?") have one edit point.

### File map (deployed branch as of this commit)

| File | Change |
|---|---|
| `hermes_cli/kanban_db.py` | `SCHEMA_SQL` adds `hub_escalation INTEGER NOT NULL DEFAULT 0`; `Task` dataclass gains the field with `from_row`; `_route_block` writes `hub_escalation=1` atomically with `status='triage'`; payload adds `quarantined` + `hub_escalation_set` flags. |
| `hermes_cli/kanban_db_connect.py` | `_LATER_TASK_COLUMNS` registers the migration for legacy boards. |
| `hermes_cli/kanban_decompose.py` | `_human_gate_match(task, comments)` central helper (covers all 5 patterns from `t_334f608b` + the quarantine pattern); `_record_decomposer_skip` audit appender; `decompose_task` runs both gates BEFORE the aux call; `list_triage_ids` filters quarantined rows. |
| `hermes_cli/kanban_specify.py` | Same quarantine gate in `specify_task`; same filter in `list_triage_ids`. |
| `tests/hermes_cli/test_kanban_decompose.py` | 7 new tests (quarantine + human-gate patterns + negative control). |
| `tests/hermes_cli/test_kanban_specify.py` | 1 new test (`specify_quarantined_card_refused`). |
| `tests/hermes_cli/test_kanban_block_kinds.py` | 3 new tests (quarantine flip, event payload, persistent across unblock). |

### Why two gates (`list_triage_ids` filter + entry-point check)?

Defense in depth:

- The `list_triage_ids` filter stops the **default dispatcher tick**.
  Without it the auto-decomposer would still pick up quarantined cards
  via `auto_decompose_tick` in
  `gateway/kanban_watchers_dispatcher.py`.
- The entry-point check stops **direct CLI invocations**. An operator
  running `hermes kanban specify t_5ca07594` manually would otherwise
  re-arm the loop.

If `list_triage_ids` is bypassed (e.g. a future caller that doesn't
go through it), the entry-point check still holds. If the entry-point
check is bypassed (e.g. a future helper), the dispatcher sweep still
holds.

## Operation

### Verifying a card is quarantined

```bash
sqlite3 ~/.hermes/kanban.db \
  "SELECT id, title, status, block_recurrences, hub_escalation \
   FROM tasks WHERE hub_escalation=1 LIMIT 20;"
```

A row with `hub_escalation=1` is one `block_task` flip away from
auto-promotion. The dispatcher will not promote it; the aux LLM will
not be called on it.

### Un-quarantining (operator hand-action)

```sql
UPDATE tasks
   SET hub_escalation=0
 WHERE id='t_<id>';
```

After this, the next dispatcher tick picks the card up automatically
(via `list_triage_ids` filter) — no further hand work required. **Do
NOT clear `hub_escalation` until you've decided the card is genuinely
executable; clearing to "fix the loop" just removes the only marker
that the loop was happening.**

### Forcing a manual specify / decompose

If the operator has decided a quarantined card is runnable, they
should:

1. Clear `hub_escalation=0` (see above).
2. Either let the next dispatcher tick pick it up, or invoke the CLI
   directly: `hermes kanban specify <id>` / `hermes kanban decompose
   <id>`.

The CLI does **not** log a `decomposer_skipped` event when the flag is
clear — the comment-thread / `task_events` view stays clean.

## Configuration

No new config keys. The breaker counter limit, the quarantine
column default, and the quarantine audit-event payload shape are all
either inherited from `BLOCK_RECURRENCE_LIMIT` (default 2) or hard-coded
in `hermes_cli/kanban_db.py`. Operators who want a different policy
edit `BLOCK_RECURRENCE_LIMIT` at the top of that file.

## Troubleshooting

### Symptom: a card with `hub_escalation=1` was promoted anyway

The entry-point check missed. Look at the call chain:

| Caller | Path |
|---|---|
| `auto_decompose_tick` (default dispatcher sweep) | `gateway/kanban_watchers_dispatcher.py` calls `list_triage_ids` → `decompose_task`. Both must filter/refuse. |
| `hermes kanban decompose <id>` (CLI) | Direct `decompose_task` call; the entry-point check must refuse. |
| `hermes kanban specify <id>` (CLI) | Direct `specify_task` call; the entry-point check must refuse. |

If none of the three paths is the cause, the card was likely un-quarantined
by hand (see `Operation` above) or by an external migrator script —
check `task_events` for an operator `unblock_task` followed by a
`specified` / `promoted` pair.

### Symptom: `hub_escalation=0` on a card that should be quarantined

Two possibilities:

1. The `UPDATE tasks SET hub_escalation=0` was issued by an operator
   (see `Operation` above). Confirm by reading `task_events`; if the
   flag flip happened, the operator intentionally un-quarantined.
2. **Branch drift regression** (the bug `t_efc7769a` was filed for).
   The `hub_escalation` column + migration + flip logic is one commit's
   worth of changes; if a future branch switch drops any piece, the
   symptom is exactly this. Verify with:

   ```bash
   cd /home/serveradmin/.hermes/hermes-agent
   git log --oneline -5
   grep -rn hub_escalation hermes_cli/ gateway/
   ```

   The deployed branch should show:

   - `kanban_db.py` — `hub_escalation INTEGER NOT NULL DEFAULT 0` in
     `SCHEMA_SQL`, `hub_escalation: bool = False` in `Task`, `=1`
     write in `_route_block`.
   - `kanban_db_connect.py` — `("hub_escalation", ...)` in
     `_LATER_TASK_COLUMNS`.
   - `kanban_decompose.py` + `kanban_specify.py` — `getattr(task,
     "hub_escalation", False)` checks, list-id filters.

   If anything is missing, you're back on the regressed branch and
   need to merge forward. See **Dependencies** below.

### Symptom: "Migration didn't run" / column missing on legacy boards

`_migrate_add_optional_columns` runs `add_column_if_missing` for every
entry in `_LATER_TASK_COLUMNS`. The helper is idempotent — running it
twice is a no-op. If a board has no `hub_escalation` column after
upgrade:

1. Open any kanban board action — `init_db` is called on first
   connect.
2. Verify:

   ```bash
   sqlite3 ~/.hermes/kanban.db \
     "PRAGMA table_info(tasks);" | grep hub_escalation
   ```

3. If the column is absent, force-add by hand:

   ```sql
   ALTER TABLE tasks
     ADD COLUMN hub_escalation INTEGER NOT NULL DEFAULT 0;
   ```

## Dependencies

- **Branch ownership:** the gates live on
  `fix/kanban-human-gate-and-quarantine-restoration` (this commit)
  off `fix/kanban-recompute-ready-blocker-gate @ d437932a74`. The 5
  gateway processes + the dispatcher run from the deployed
  `fix/kanban-recompute-ready-blocker-gate` branch — switching them to
  this branch is a **Ultron-owned decision** (deployment gate, not
  code gate). Filed as a child card `t_<see kanban>` for Ultron. Until
  the gateway processes restart on the new branch, the deploy is
  STILL MISSING the gate and the structural loop can re-arm in
  production — `t_5ca07594` is the canonical victim.
- **Branch drift failure mode (the one this page documents):**
  `2026-09-17 22:27` checkout from
  `fix/state-db-busy-timeout` to
  `fix/kanban-recompute-ready-blocker-gate` overwrote
  `kanban_db.py`, `kanban_decompose.py`, `kanban_specify.py`, and
  `kanban_watchers_dispatcher.py`. The DB-level `recompute_ready`
  guard (t_5fe84da5, `eb9843d891`) survived because it only touches
  `kanban_db.py` lines that were cherry-picked cleanly. The
  decomposer/specifier-level guards did NOT survive. Operators: when
  switching branches, ALWAYS `git diff <old>..<new> -- hermes_cli/
  gateway/` and confirm the canonical kanban-quarantine code path is
  still present.

## See Also

- `Hermes/Services/Kanban-Block-Loop-Breaker.md` — the loop breaker
  itself (`recompute_ready`, `block_recurrences`,
  `BLOCK_RECURRENCE_LIMIT`).
- `Hermes/Memory/Friday/2026-08-28-circuit-breaker-quarantine.md` —
  the original August 28 quarantine writeup.
- `Hermes/Memory/Friday/2026-09-23-branch-drift-regression-t_efc7769a.md`
  — the September 23 restoration writeup.

## Change log

- 2026-08-28 — Initial quarantine gates added (`t_8b48a01f`,
  `t_334f608b`). Commits `881677364d` and `4ed35e090e` on
  `fix/state-db-busy-timeout`.
- 2026-09-17 22:27 — Branch drift regression: switching from
  `fix/state-db-busy-timeout` to
  `fix/kanban-recompute-ready-blocker-gate` overwrote
  `kanban_db.py` / `kanban_decompose.py` /
  `kanban_specify.py` /
  `kanban_watchers_dispatcher.py`. The recompute_ready DB-level guard
  survived; the decomposer/specifier-level guards did NOT.
- 2026-09-23 — Gates restored via surgical edits + 11 regression
  tests + this wiki page (`t_efc7769a`). Branch:
  `fix/kanban-human-gate-and-quarantine-restoration` (this commit).
