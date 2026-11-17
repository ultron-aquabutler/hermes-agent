# Kanban Invalid-Status Reconciliation

The invalid-status sweep is the Kanban dispatcher's backstop for cards parked on a
status the dispatcher does not understand — `in_progress` being the one seen in
production. It answers one question on every sweep: *is there a card whose
`status` is outside the workflow, and if so, what is the defensible thing to do
with it?*

Written 2026-11-17 by @friday, alongside the fix for the permanently-hidden
`in_progress` cards (kanban `t_d93f62bf`).

## Overview

Every status-filtered query in the dispatcher matches a **literal**:

| Query | Matches | Purpose |
|---|---|---|
| `release_stale_claims` | `status = 'running'` | reclaim expired claims |
| `detect_crashed_workers` | `status = 'running'` | requeue dead workers |
| `detect_stale_running` | `status = 'running'` | requeue stalled workers |
| dispatch / spawn cap | `status = 'ready'` | pick up work |
| `reconcile_orphaned_running` | `status = 'running'` | repair broken claim bookkeeping |

A card parked on a status outside the workflow matches **none** of them. It
cannot be dispatched, cannot be completed (the transition guard requires a known
source status), and cannot be reclaimed. It is a permanent orphan that only a raw
`SELECT` can find.

That is what happened on the `mcp-infra` board. Three cards carried
`status = 'in_progress'` from **2026-11-03** onward:

| Card | Assignee | Created |
|---|---|---|
| `t_0ef6a359` | ultron | 2026-11-03 |
| `t_macbook_ssh_sso_rollout` | — | 2026-11-03 |
| `t_dc1_dc2_replication_2026_09_14` | ultron | 2026-11-03 |

Because Mission Control's snapshot writer skips cards whose status its
`kanban_card_snapshot_status_check` CHECK constraint rejects, those three never
appeared on the Kanban tab, and the Phase 2f threshold alerting fired a warning
every snapshot cycle:

```
# Kanban task t_d93f62bf: [WARNING] ⚠️ 3 kanban card(s) hidden by unknown status `in_progress`
```

The same board also carried three cards on `pending`/`completed`. Those were
NOT reported, because `cancelled` is filtered server-side in
`app/snapshot/kanban.py` and `pending`/`completed` are accepted by the CHECK
constraint added in migration `018_expand_status_check_scheduled.sql`. Only
`in_progress` was both rejected by the constraint and reported by the
anomaly detector — deliberately, so the alert keeps firing until the defect is
fixed.

### The write paths all validate

`create_task` checks `VALID_INITIAL_STATUSES`; the dashboard plugin whitelists
status verbs before writing; every public transition in `kanban_db.py` uses a
hard-coded status. The value reached the board from **outside** every validated
path — direct SQL, a DB restore, or a tool predating the workflow.

That makes this sweep a **backstop, not the primary fix**. It exists so such a
card self-heals instead of haunting the board forever. It is deliberately
status-agnostic: any value outside `VALID_STATUSES` is handled, not just
`in_progress`.

## Architecture

| Piece | Location | Role |
|---|---|---|
| `reconcile_invalid_status()` | `hermes_cli/kanban_db_dispatch.py` | Finds and repairs cards outside the workflow |
| `reconcile_invalid_status` field | `DispatchResult` | Reports the repaired task ids |
| `reconciled_invalid_status` | `_TICK_ACTIVITY_FIELDS` (`hermes_cli/kanban_db.py`) | Makes the tick count as activity |
| `VALID_STATUSES` | `hermes_cli/kanban_workflow.py` (via `DEFAULT_STATUSES`) | The authoritative set of valid statuses |

The sweep runs in the reclaim phase of `dispatch_once`, immediately after
`reconcile_orphaned_running`, and is gated by the same
`kanban.reconcile_orphans` config key (default on). It is therefore
**OFF wherever orphan reconciliation is OFF**.

### Disposition rules

In order of preference:

| Condition | Target | Why |
|---|---|---|
| Last run **completed** | `done` | the work is finished; the status was the only thing wrong |
| A **live worker** on this host owns the card | *(deferred)* | never flip a card out from under a running worker; retried next tick |
| A run exists and **ended** without completing | `ready` | unfinished work — put it back in the dispatch path |
| ...but a **parent is unfinished** | `todo` | `ready` would let the dispatcher spawn a child whose upstream has not completed |
| **No run at all** | `blocked` | no evidence of work in flight and no owner to infer a disposition from |

The `no run at all → blocked` case is the important one. Auto-completing it
would fabricate a result; auto-requeueing it would put a card nobody wrote into
the spawn path. Blocking surfaces it for operator triage, which is the only
honest disposition when the evidence is absent.

### Safety guards

The sweep inherits the same protections as the orphan sweep:

- **`owner_reclaim_blocked()`** — an attempt whose effects may still be in
  flight owns the card until it is provably over.
- **`_worker_alive()`** — a live worker PID on this host defers the repair.
- **`_reclaim_dangling_run()`** — closes a leaked open run before the status
  flip, so the invariant `current_run_id IS NULL <=> run row terminal` holds.
- **Compare-and-set UPDATE** — `WHERE id = ? AND status = ?` with the status
  read inside the write transaction, so an operator's concurrent fix is not
  clobbered.

Every repair writes a `dispatcher` comment naming the offending status and a
`reconciled` event with `reason: "invalid_status"`, so the action is auditable.

## Operation

The sweep is automatic. No operator action is required.

To see which cards on a board currently carry a status outside the workflow:

```bash
sqlite3 -header -column \
  "file:/home/serveradmin/.hermes/kanban/boards/<board>/kanban.db?mode=ro" \
  "SELECT id, status, assignee FROM tasks
    WHERE status NOT IN ('triage','todo','scheduled','ready','running',
                         'blocked','review','done','archived');"
```

To inspect a repaired card's audit trail:

```bash
sqlite3 -header -column \
  "file:/home/serveradmin/.hermes/kanban/boards/<board>/kanban.db?mode=ro" \
  "SELECT task_id, body FROM task_comments
    WHERE task_id = '<task_id>' ORDER BY id DESC LIMIT 5;"
```

To inspect the repair events:

```bash
sqlite3 -header -column \
  "file:/home/serveradmin/.hermes/kanban/boards/<board>/kanban.db?mode=ro" \
  "SELECT kind, payload FROM task_events
    WHERE task_id = '<task_id>' AND kind = 'reconciled' ORDER BY id DESC;"
```

### Verified behaviour

An end-to-end run of the real sweep against a **copy** of the live `mcp-infra`
board produced:

```
=== BEFORE ===
  t_0ef6a359  status=in_progress  assignee=ultron run_id=None pid=None
  t_macos_bind_redeploy  status=pending      assignee=None run_id=None pid=None
  t_macos_bind_done_status  status=completed    assignee=None run_id=None pid=None
  t_macbook_ssh_sso_rollout  status=in_progress  assignee=None run_id=None pid=None
  t_dc1_dc2_replication_2026_09_14  status=in_progress  assignee=ultron run_id=None pid=None
  t_deadpool_pbs_restore_2026_09_14  status=pending      assignee=default run_id=None pid=None

=== RUNNING SWEEP ===
  repaired: ['t_0ef6a359', 't_macos_bind_redeploy', 't_macos_bind_done_status',
             't_macbook_ssh_sso_rollout', 't_dc1_dc2_replication_2026_09_14',
             't_deadpool_pbs_restore_2026_09_14']

=== AFTER ===
  t_0ef6a359  status=ready        run_id=None pid=None
  t_macos_bind_redeploy  status=blocked      run_id=None pid=None
  t_macos_bind_done_status  status=blocked      run_id=None pid=None
  t_macbook_ssh_sso_rollout  status=blocked      run_id=None pid=None
  t_dc1_dc2_replication_2026_09_14  status=blocked      run_id=None pid=None
  t_deadpool_pbs_restore_2026_09_14  status=blocked      run_id=None pid=None

=== REMAINING non-standard rows ===
  count: 0

=== IDEMPOTENCY: second pass ===
  repaired: []
```

`t_0ef6a359` had run history and was requeued to `ready`; the other five had no
run history and were blocked for triage. The second pass found nothing — the
sweep is idempotent.

The live board was **not** modified by that verification; only the copy was
written.

## Configuration

| Knob | Default | Where |
|---|---|---|
| `kanban.reconcile_orphans` | `true` | `config.yaml` — gates the sweep along with orphan reconciliation |
| Valid status set | `DEFAULT_STATUSES` + `archived` | `hermes_cli/kanban_workflow.py` |

There is no separate switch for the invalid-status sweep. Sharing the
`reconcile_orphans` gate is deliberate: both are backstops for the same class
of bookkeeping divergence, and an operator who disables reconciliation has
said they do not want the dispatcher moving cards on its own judgement.

## Troubleshooting

### A card is still hidden from Mission Control

The sweep moves the card to a **valid** status. If the card is still
invisible, the sweep is not running:

1. Confirm the card's status is actually outside the workflow:
   ```bash
   sqlite3 "file:/home/serveradmin/.hermes/kanban/boards/<board>/kanban.db?mode=ro" \
     "SELECT id, status FROM tasks WHERE id = '<task_id>';"
   ```
2. Confirm the running gateway loaded the fix. The dispatcher is **in-process**,
   so a code change needs a gateway restart to take effect:
   ```bash
   cd /home/serveradmin/.hermes/hermes-agent
   git log -1 --format='%h %s'
   systemctl --user show hermes-gateway.service -p ActiveState
   ```
3. Confirm reconciliation is not disabled:
   ```bash
   grep -n "reconcile_orphans" /home/serveradmin/.hermes/profiles/<profile>/config.yaml
   ```
4. Check the dispatcher log for the repair line:
   ```
   kanban reconcile: repaired task <task_id> with invalid status 'in_progress' -> <target>
   ```

### Mission Control still reports "N kanban card(s) hidden by unknown status"

That alert counts cards the MC snapshot writer **skipped**, not cards on an
invalid status. It is a snapshot-side count and will not drop until the next
MC snapshot run after the cards are repaired. Confirm the alert's live state
against Mission Control's Postgres (read-only role):

```bash
# From the mission-control checkout, using the mc_ro secret.
# SELECT id, dedupe_key, severity, fired_at, cleared_at, occurrences
#   FROM mc.alert_history WHERE dedupe_key = 'kanban_unknown_status:in_progress'
```

### A card was blocked by the sweep and you disagree with the disposition

Read the comment the sweep wrote — it names the offending status and the rule
that fired:

```bash
sqlite3 "file:/home/serveradmin/.hermes/kanban/boards/<board>/kanban.db?mode=ro" \
  "SELECT body FROM task_comments WHERE task_id = '<task_id>' ORDER BY id DESC LIMIT 1;"
```

Then move the card to the correct status with the normal workflow — **do not**
hand-edit the kanban database:

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python3 -m hermes_cli.main kanban <command> <task_id> "<reason>"
```

### `PermissionError: delegate_task child contexts cannot mutate Kanban tasks or boards`

Expected when running the sweep from a `delegate_task` child. The guard is
fail-closed by design: descendants may not mutate boards. Run it from the
board owner's own context instead.

## Dependencies

- `hermes_cli/kanban_db_dispatch.py` — the sweep.
- `hermes_cli/kanban_db.py` — `write_txn`, `_reclaim_dangling_run`,
  `_parents_satisfied`, `_end_run`, `_insert_comment`, `_append_event`, and the
  `DispatchResult` counter list.
- `hermes_cli/kanban_workflow.py` — `DEFAULT_STATUSES` (the valid status set).
- `hermes_cli/kanban_owner_recovery.py` — `owner_reclaim_blocked` (the
  in-flight-attempt guard).
- `hermes_cli/kanban_db_connect.py` — `write_txn` (IMMEDIATE transactions) and
  the delegated-child write guard.
- `tests/gateway/test_kanban_reconcile_invalid_status.py` — the sweep's
  behaviour-contract tests.

## See Also

- `docs/wiki/services/kanban-rate-limit-retry-budget.md` — the other
  dispatcher-side backstop that parks cards for operator triage.
- `docs/wiki/services/deploy-branch-guard.md` — the guard that can stop a
  board from dispatching at all.
- `Homelab/10-Services/Kanban/` — vault copy of the Kanban service notes.

## Change History

| Date | Change | Commit |
|---|---|---|
| 2026-11-17 | Sweep added: `reconcile_invalid_status()` finds cards whose `status` is outside `VALID_STATUSES` and moves them to a status the dispatcher understands. Fixes the permanent hiding of the three `mcp-infra` `in_progress` cards. | (this commit) |
