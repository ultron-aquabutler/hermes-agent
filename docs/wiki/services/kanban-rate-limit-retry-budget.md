# Kanban Rate-Limit Retry Budget

The rate-limit retry budget is the bounded backstop on the Kanban dispatcher's
quota-wall requeue path. It answers one question on every sweep: *has this card
already burned through its allowance of rate-limited retries?* — and if so, it
trips the circuit breaker so the card stops being re-dispatched and reaches a
human instead.

Written 2026-11-16 by @friday, alongside the fix for the unbounded rate-limit
requeue loop (kanban `t_911cf02a`).

## Overview

A worker that hits a provider quota wall exits with a dedicated sentinel exit
code (`KANBAN_RATE_LIMIT_EXIT_CODE`). The dispatcher reclaims that dead run and
releases the card back to `ready` so it can be retried later. That behaviour is
correct in isolation — a quota wall is not the worker's fault, and it is not the
card's fault either.

The defect was that **nothing bounded it**. The rate-limit path deliberately
does *not* charge `consecutive_failures`, so the ordinary circuit breaker never
saw it. The card was released to `ready` on every cooldown expiry and
re-dispatched forever.

Observed on `t_911cf02a` ("Implement isolated EZO carrier PCB layout"):

| Signal | Value |
|---|---|
| Total attempts | 95 |
| `rate_limited` outcomes | 76 |
| `crashed` outcomes | 13 |
| `timed_out` outcomes | 5 |
| `gave_up` | 1 |
| `consecutive_failures` after 95 attempts | **1** |
| Default-profile token burn | ~5.76M input / ~1.12M output tokens over 3 days |

Each attempt re-loaded the full card and repo context into the assignee's
profile. The last run ended with *"Iteration budget exhausted (150/150)"*.

### The bug was fleet-wide, not one card

`t_911cf02a` was simply the longest-running survivor. A board-wide census found
**twelve** cards carrying 831–874 rate-limited retries each, all spanning
**2026-11-12 02:25 → 2026-11-15 23:57** — a sustained fleet-wide quota wall that
hit every worker at once:

| Card | Rate-limited retries | Terminal state |
|---|---|---|
| `t_60858e28` | 874 | done |
| `t_a561fbcf` | 874 | done |
| `t_0b096051` | 873 | done |
| `t_b8440f76` | 873 | done |
| `t_2c268716` | 871 | done |
| `t_4b64e02d` | 870 | done |
| `t_d2cfb690` | 868 | done |
| `t_84af873d` | 865 | done |
| `t_82a6e967` | 861 | blocked |
| `t_b58061dd` | 842 | blocked |
| `t_14b09875` | 837 | done |
| `t_c383afe0` | 831 | done |

Every one of them shows `consecutive_failures` of **0 or 2** — the breaker never
saw the rate limits at all. Those cards reached `done`/`blocked` only because
their *other* outcomes eventually tripped the ordinary breaker, not because the
rate-limit path was bounded.

### What this is NOT

This budget is **not** the protocol-violation budget. That one
(`_PROTOCOL_VIOLATION_SCAN_LIMIT` / `_protocol_violation_streak`) counts
consecutive worker protocol violations — a worker exiting without calling
`kanban_complete` or `kanban_block` — and is unchanged by this work.

It is also **not** the fix for the August 2026 completed/crashed alternation
seen on `t_1406958d`, `t_d59e3496`, `t_70f71747`, `t_6732317a`, `t_980e4e73`
(958–5,002 attempts each). Those events are all `completion_rejected_fpd`
("claimed files missing on disk") from a **separate, already-fixed** vault-path
verification subsystem. Every one of those events is dated 2026-08-20 through
2026-08-26; none occur in November. Breaking a violation streak on a genuinely
completed run is correct behaviour, not a bug.

## Architecture

| Piece | Location | Role |
|---|---|---|
| `_RATE_LIMIT_RETRY_LIMIT` | `hermes_cli/kanban_db_dispatch.py` | Default consecutive rate-limit allowance (5) |
| `_RATE_LIMIT_SCAN_LIMIT` | `hermes_cli/kanban_db_dispatch.py` | Closed runs walked when counting the streak (50) |
| `_rate_limit_streak()` | `hermes_cli/kanban_db_dispatch.py` | Counts the trailing run of consecutive `rate_limited` closed runs |
| `_account_rate_limits()` | `hermes_cli/kanban_db_dispatch.py` | Charges the budget; trips the breaker when the streak reaches the limit |
| `_record_task_failure(..., force_trip=True)` | `hermes_cli/kanban_db_dispatch.py` | Existing breaker primitive, reused unchanged |

The sweep collects rate-limited task IDs into `sweep.rate_limited` inside
`detect_crashed_workers`. After the main transaction closes, `_account_rate_limits`
runs **outside** it — the same shape as `_account_crashes`, which is likewise
outside the transaction because `_record_task_failure` opens its own
`write_txn`. Calling it inside the transaction raises
`RuntimeError: write_txn: already inside a transaction`.

### The streak is consecutive-only

`_rate_limit_streak()` walks closed runs newest-first. A `rate_limited` run
extends the streak; **any** other closed run breaks it — a success, a crash, a
protocol violation. All three mean the worker got past the quota wall at least
once, so the card's budget resets. This is deliberately the same shape as
`_protocol_violation_streak`.

The consequence worth stating plainly: a card that alternates
`rate_limited → success → rate_limited → success` never trips. That is
correct — it is making progress.

### Per-task override

A task's `max_retries` overrides the default allowance, matching the
protocol-violation budget's behaviour. A card that legitimately needs more
quota-wall retries can ask for them.

## Operation

The budget is automatic. No operator action is required for it to work.

To observe a card's current rate-limit streak against the live board:

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python3 -c "
import sqlite3, sys
sys.path.insert(0, '.')
from hermes_cli import kanban_db_dispatch as kd
c = sqlite3.connect(
    'file:/home/serveradmin/.hermes/kanban/boards/homelab/kanban.db?mode=ro',
    uri=True,
)
c.row_factory = sqlite3.Row
for t in ('t_911cf02a',):
    print(t, 'streak =', kd._rate_limit_streak(c, t))
"
```

To see which cards carry a rate-limit history:

```bash
sqlite3 -header -column \
  "file:/home/serveradmin/.hermes/kanban/boards/homelab/kanban.db?mode=ro" \
  "SELECT task_id, count(*) AS rate_limited
     FROM task_runs WHERE outcome = 'rate_limited'
    GROUP BY task_id ORDER BY rate_limited DESC LIMIT 10;"
```

When the budget is exhausted the card is parked `blocked` with a
`last_failure_error` naming the quota wall, so it surfaces in the normal
blocked-card review path.

### Verified behaviour

An end-to-end run against an isolated board, driving the production
`detect_crashed_workers()` sweep path, produced:

```
budget limit = 5
hit 1: status='ready'   consecutive_failures=0  streak=1  counted_as_crash=False
hit 2: status='ready'   consecutive_failures=0  streak=2  counted_as_crash=False
hit 3: status='ready'   consecutive_failures=0  streak=3  counted_as_crash=False
hit 4: status='ready'   consecutive_failures=0  streak=4  counted_as_crash=False
hit 5: status='blocked' consecutive_failures=1  streak=5  counted_as_crash=False
E2E PASS: card parked blocked at the budget, sticky, no open runs
```

Three properties are worth reading off that trace:

1. Under the budget the card requeues `ready` with **zero** failures charged.
2. At the budget it parks `blocked`, and the trip charges exactly **one**
   failure — not one per rate-limited requeue.
3. `counted_as_crash=False` throughout: a quota wall is never booked as a
   worker crash.

The block is sticky — `recompute_ready()` does not resurrect a
budget-exhausted card — and the card is left with no open runs.

### Parking a hot card manually

For an immediate stop while the code fix is pending, park the card with the
normal workflow — do not hand-edit the kanban database:

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python3 -m hermes_cli.main kanban schedule <task_id> \
  "Parked: unbounded rate-limit requeue loop. Awaiting retry-budget fix."
```

`kanban schedule` moves the card to `scheduled`, which the dispatcher will not
pick up. It remains visible and can be resumed by an operator.

If the card is already `scheduled`, the command prints
`cannot schedule <task_id>` and changes nothing — that is the idempotent
outcome, not an error to chase. Check the card's status first with the
troubleshooting queries below.

## Configuration

| Knob | Default | Where |
|---|---|---|
| `_RATE_LIMIT_RETRY_LIMIT` | 5 consecutive rate-limited requeues | `hermes_cli/kanban_db_dispatch.py` |
| Per-card override | card's `max_retries` | `tasks.max_retries` column |
| Respawn cooldown | unchanged — rate-limited cards still wait out the quota cooldown before re-dispatch | `hermes_cli/kanban_db.py` |

The cooldown behaviour is untouched. The budget is an **attempt-count** bound,
not a time bound: a card may legitimately wait hours between retries, but it
cannot retry forever.

## Troubleshooting

### A card is `blocked` with a rate-limit error and you did not expect it

The budget tripped. Check the streak and the card's `max_retries`:

```bash
sqlite3 -header -column \
  "file:/home/serveradmin/.hermes/kanban/boards/homelab/kanban.db?mode=ro" \
  "SELECT id, status, consecutive_failures, max_retries, last_failure_error
     FROM tasks WHERE id = '<task_id>';"
```

`consecutive_failures` will be **low** even when the card has been
rate-limited many times — that is the whole point of the separate budget,
and it is the signal that distinguishes a rate-limit loop from a crash loop.

Specifically: while the card is **under** the budget, `consecutive_failures`
stays at **0**. When the budget trips, the trip itself charges exactly
**one** failure — not one per rate-limited requeue. A card that burned five
rate-limited requeues therefore ends up with `consecutive_failures = 1`, not
`5`. If you see `consecutive_failures` climbing in lockstep with the number
of rate-limited runs, the budget is not the path that handled that card.

### A card is re-dispatching far more often than expected

Confirm the outcome mix. `rate_limited` means the budget should eventually
stop it; `crashed` means you are looking at a different failure mode (worker
crash or protocol violation), which has its own breaker.

```bash
sqlite3 -header -column \
  "file:/home/serveradmin/.hermes/kanban/boards/homelab/kanban.db?mode=ro" \
  "SELECT outcome, count(*) FROM task_runs
    WHERE task_id = '<task_id>' GROUP BY outcome ORDER BY 2 DESC;"
```

### A card with many rate-limits never trips the budget

Check whether non-rate-limited runs are interleaved. The streak is
consecutive-only by design:

```bash
sqlite3 "file:/home/serveradmin/.hermes/kanban/boards/homelab/kanban.db?mode=ro" \
  "SELECT id, outcome FROM task_runs WHERE task_id = '<task_id>'
    ORDER BY id DESC LIMIT 20;"
```

If a `completed` or `crashed` run sits between the `rate_limited` runs, the
streak resets at that point. A card that genuinely recovers should not carry an
old quota-wall budget forever.

### `RuntimeError: write_txn: already inside a transaction`

A regression that means `_account_rate_limits` was called from inside the
sweep's transaction. It must run outside — see *Architecture* above.

## Dependencies

- `hermes_cli/kanban_db_dispatch.py` — the sweep, the streak counter, and the
  breaker trip.
- `hermes_cli/kanban_db.py` — `_record_task_failure`, `consecutive_failures`
  accounting, the respawn cooldown, and `KANBAN_RATE_LIMIT_EXIT_CODE`.
- `hermes_cli/kanban_db_connect.py` — `write_txn` (IMMEDIATE transactions).
- `tests/hermes_cli/test_kanban_db.py` — the rate-limit test group.

## See Also

- `docs/wiki/services/deploy-branch-guard.md` — the other dispatcher guard that
  can stop a board from dispatching.
- `docs/wiki/services/managed-worker-cgroup-isolation.md` — keeps dispatcher
  workers out of the gateway's memory cgroup.
- `Homelab/10-Services/Gateway/managed-worker-cgroup-isolation.md` — vault copy.

## Change History

| Date | Change | Commit |
|---|---|---|
| 2026-11-16 | Bounded rate-limit retry budget added: `_rate_limit_streak()`, `_account_rate_limits()`, `_RATE_LIMIT_RETRY_LIMIT=5`. Fixes the unbounded quota-wall requeue loop on `t_911cf02a` (95 attempts, breaker never tripped). | see `git log` for `kanban_db_dispatch.py` |
