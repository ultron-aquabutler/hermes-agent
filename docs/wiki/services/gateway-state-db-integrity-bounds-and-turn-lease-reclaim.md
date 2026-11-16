# Gateway state.db Integrity Bounds + Stale Turn-Lease Reclaim (t_e0a466c4)

> Wiki page for the F2+F4 port onto the live gateway branch.
>
> Status: **PORTED ONTO THE LIVE BRANCH — NOT YET ACTIVE IN THE RUNNING GATEWAY.**
>
> The fixes were originally written and tested on `canonical-deploy`
> (commit `e6cf1a3fca`). That branch is **not** the tree the running gateway
> executes: the gateway imports from `/home/serveradmin/.hermes/hermes-agent`,
> checked out on the live branch `canonical-deploy-live`. The original commit
> `eab5568fb2` was a rebase casualty (dangling — `git branch -a --contains`
> returns nothing), so the fixes existed in the repo but not in the running
> code. This card ports them by hand onto the live branch.
>
> **No gateway restart has been performed.** The tree carries the fix, but
> in-flight and future turns pick it up only when the gateway is restarted.
> A restart reclaims the unit's cgroup and kills in-flight turns — it is a
> separate, human-gated step for a maintenance window.
>
> Evidence that dates the switch: `gateway.log.1` contains many F2-only
> verdicts (`skipped-too-large`), the last at **2026-11-09 21:46:29**. The
> current gateway booted **2026-11-12 02:25:29** and has logged **zero**
> integrity verdicts (`grep -c 'skipped-too-large' logs/gateway.log` → 0).

## Overview

Two independent faults could wedge the gateway against its own state.db. Both
existed on `canonical-deploy` only; both are now on the live branch.

### F2 — the unclean-exit integrity check could block the boot

`PRAGMA quick_check(1)` is **O(store size)**. On a multi-GB `state.db` it ran
for tens of minutes. The check rides the boot path (`record_startup`), so its
runtime **is** the outage: the API server does not listen until the check
returns.

The live tree already deferred the check off the boot path
(`record_startup(defer_integrity_check=True)` → a daemon thread with
`deadline_s=1800`). That deferral was not a bound. The abort rode SQLite's
**progress handler**, which only fires inside the VDBE loop — on the real
3.3 GB store the first progress callback took **93 seconds** to arrive, so the
effective abort landed far past the nominal 1800 s budget.

F2 bounds it twice over:

- **A size ceiling.** `_INTEGRITY_CHECK_MAX_BYTES = 1_073_741_824` (1 GiB).
  A larger store is **not checked at all** — verdict `skipped-too-large`,
  decided with **no I/O against the store**.
- **A caller-side wall-clock deadline** enforced from a **worker thread**,
  not from inside SQLite. A check that outlives its budget is abandoned on
  time — verdict `timeout`.

The two new verdicts mean **"the check did NOT RUN"**, which is a different
answer from "the store is corrupt". They are logged as *inconclusive*
(WARNING), never as a corruption finding (ERROR).

### F4 — a stale turn lease held by a live PID was unreclaimable

A durable turn lease row (`session_turn_leases`) records a holder string:

```
pid=3860160:pidns=4026531836:turn=<session_id>:<task_id>:<uuid8>:platform=telegram
```

The reclaim predicate could only prove a holder stale through its `pid=`
component. A lease whose **turn died** while the owning **process stayed
alive** was indistinguishable from a live turn, so the row survived until its
TTL — and every later inbound on that session waited it out.

That is not theoretical. The 2026-10-17 / 2026-10-23 outages left rows held
by `pid=3860160`, the **live** gateway process. The turn had ended or wedged
and never unwound; the PID probe read "alive" because the *gateway* was
alive.

F4 makes the turn itself provably over: a per-turn **liveness registry**
records the tokens this process minted and retired, so a holder naming a turn
this process already finished is reclaimable **even though its PID is alive**.

## Architecture

### F2 — the bounded integrity check

```
gateway/lifecycle_ledger.py
  check_state_db_integrity(home, *, deadline_s=None)
    -> _store_exceeds_integrity_ceiling(path)      # stat() only, no I/O
       size > _INTEGRITY_CHECK_MAX_BYTES  ->  "skipped-too-large"
    -> _integrity_check_timeout_s()                # env override, else default
    -> _invoke_quick_check(path, deadline, deadline_at)
         -> _run_quick_check(path, deadline, deadline_at=deadline_at)
              worker thread: sqlite3.connect -> PRAGMA quick_check(1)
              bounded join: thread.join(timeout=...)
              thread still alive  ->  ("timeout", None)
```

Two deliberate design points:

- **The deadline is enforced by the caller, not by SQLite.** The progress
  handler's granularity is the whole problem; running the check on a worker
  thread lets the boot path abandon it on time regardless.
- **The worker thread is a daemon and is deliberately NOT joined on
  timeout.** Joining it would re-block the boot for exactly the duration
  being avoided. The abandoned check only reads.

`_invoke_quick_check` inspects `_run_quick_check`'s **signature** (not by
catching `TypeError`) so a narrower test stub is tolerated without a
`TypeError` raised inside a real check triggering a second, unbounded call.

### F4 — the turn liveness registry

```
agent/turn_liveness_registry.py          # new, process-local
  register_turn_token(token)              # a turn of THIS process is running
  retire_turn_token(token)                # the turn is over
  holder_turn_token(holder)               # extract the token from a holder
  holder_turn_is_dead(holder)             # True only for a KNOWN retired token

agent/turn_facade_lease.py
  admit_durable_turn_lease(...)          -> register_turn_token(relay_turn_id)
                                           (retire it again if admission fails)
  DurableTurnLease.stop_refresher()      -> retire_turn_token(self._turn_token())
  DurableTurnLease.refresh_tick()        -> if _turn_thread_is_gone(): release the row

hermes_state.py
  _compression_lock_holder_process_is_dead(holder)
      -> _turn_lease_holder_turn_is_dead(holder)   # turn-token check FIRST
      -> then the existing pid= probe
```

The reclaim seam is the point: `hermes_state_compression.py` imports
`_compression_lock_holder_process_is_dead` **from `hermes_state`**, so the
turn-token check is added at the one function both the acquire path and the
sweep path read.

### Why the registry is safe

- **Process-local.** Only a holder minted by THIS process can be retired here.
  A lease held by another process keeps deferring to its TTL — this never
  steals a live turn from a sibling process.
- **Fail-safe.** An unknown token is **not** treated as dead. A holder we
  cannot vouch for keeps deferring to TTL expiry, which self-heals. A false
  reclaim would end a live turn; a late reclaim merely delays the next
  inbound.
- **Turn-scoped, not generation-scoped.** The registry is keyed by the unique
  per-turn token already present in the holder string, so a successor turn on
  the same session is never affected by its predecessor's retirement.

### The token cannot be colon-delimited

The token is `f"{session_id}:{task_id}:{uuid4hex8}"` and **both parts may
contain `:`** (a session id is fingerprint-derived; a task id is a uuid). It is
therefore delimited by the next **known structured component** — `:platform=`
for turn holders, `:cron:` for cron holders — not by a fixed colon count.

### The refresher's own escape hatch

`DurableTurnLease.refresh_tick()` also checks `_turn_thread_is_gone()`: the
agent stamps `_execution_thread_id` at the start of every turn, so a turn
thread that has exited proves the turn is over even if the façade's `finally`
never ran. When it is, the refresher stops renewing and releases the row so
the next turn can proceed immediately.

It is conservative by construction: an unset thread id, an id that still
names a live thread, or any error reading the thread table all answer False
(keep renewing). Only a positively-proven dead thread reclaims the lease.

## Operation

### Verify the fix is on the live branch

```bash
cd /home/serveradmin/.hermes/hermes-agent
git rev-parse --abbrev-ref HEAD                 # canonical-deploy-live
git grep -c '_INTEGRITY_CHECK_MAX_BYTES' -- gateway/lifecycle_ledger.py
git cat-file -e HEAD:agent/turn_liveness_registry.py && echo PRESENT
git grep -c 'retire_turn_token' -- agent/turn_facade_lease.py
```

All three probes must be non-zero / PRESENT. **A fix that is green on a
branch the gateway does not execute is not a fix** — that is exactly how this
card came to exist.

### Run the regression tests

```bash
cd <worktree>
scripts/run_tests.sh tests/agent/test_turn_lease_stale_reclaim.py
scripts/run_tests.sh tests/gateway/test_lifecycle_integrity_check_bounds.py
```

### Watch the verdicts

```bash
grep -E 'skipped-too-large|INCONCLUSIVE|integrity check' ~/.hermes/logs/gateway.log
```

`skipped-too-large` and `INCONCLUSIVE` are expected on a multi-GB store.
A `FAILED integrity check` line means a real corruption finding.

### Verify the reclaim path end-to-end

```bash
sqlite3 ~/.hermes/state.db \
  "SELECT conversation_id, holder, expires_at FROM session_turn_leases;"
```

A row whose `holder` names a turn token this process retired is reclaimable
on the next acquire. A row whose holder is a sibling process's token defers
to `expires_at`.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `_INTEGRITY_CHECK_MAX_BYTES` | `1_073_741_824` (1 GiB) | Store size above which the quick_check is skipped. Decided with no I/O. |
| `_INTEGRITY_CHECK_TIMEOUT_S` | `120.0` s | Wall-clock budget for one check. |
| `HERMES_STATE_DB_INTEGRITY_CHECK_TIMEOUT_S` | unset | Overrides the budget. Invalid or non-positive values fall back to the default. |
| `LEASE_TTL_SECONDS` | `300.0` s | Turn-lease TTL. The fallback path for anything the registry cannot prove. |
| `_TURN_LEASE_SWEEP_GRACE_S` | `86400.0` s (24 h) | How long an expired row is retained before the sweep deletes it. |

The timeout budget is generous enough for a healthy store of any plausible
size (quick_check on a 500 MB store is ~2 s) and is overridable for operators
who want a thorough check on a large store.

## Troubleshooting

### Symptom: the gateway restart takes many minutes and the API server never listens

The integrity check is on the boot path. Check the verdict:

```bash
grep -E 'skipped-too-large|INCONCLUSIVE|quick_check exceeded' ~/.hermes/logs/gateway.log
```

If the store is above the ceiling the verdict is `skipped-too-large` and the
check did **not** run — the boot is not blocked. If the check itself is
slow, raise the budget with
`HERMES_STATE_DB_INTEGRITY_CHECK_TIMEOUT_S`, or run `hermes doctor` for a
full check outside the boot path.

### Symptom: `state.db FAILED integrity check after an unclean gateway exit`

This is a **real corruption finding**, distinct from `timeout` /
`skipped-too-large`. Run `hermes doctor`. Do not mistake the two classes:
the inconclusive verdicts are logged at WARNING and say the store was *not
verified*, not that it is *broken*.

### Symptom: an inbound on a session hangs waiting for "Another Hermes process is using this session"

A stale turn lease is still held. Check the row:

```bash
sqlite3 ~/.hermes/state.db \
  "SELECT conversation_id, holder, expires_at FROM session_turn_leases;"
```

- **holder names a turn this process retired** → reclaimable on the next
  acquire; the fix handles it.
- **holder names another process's turn** → defers to TTL by design (never
  steal a sibling's live turn).
- **row is older than `_TURN_LEASE_SWEEP_GRACE_S`** → the sweep deletes it
  when the same `conversation_id` is re-acquired.

There is also an hourly fleet-wide mitigation:
`hermes-turn-lease-reclaim.timer` →
`~/.hermes/scripts/reclaim_stale_leases.py`, which is **liveness-gated** (it
never deletes a live in-flight turn's lease). The runbook is
`~/.hermes/skills/workflow/agent-gateway-recovery/references/gateway-self-wedge-recovery.md`.

### Symptom: a live turn is being reclaimed out from under itself

That would be a bug in the registry's fail-safe direction. The registry only
reports a token dead when **this process** minted AND retired it. Check
whether the turn is retiring its token on the normal path: `stop_refresher()`
retires it, and admission retires it again when the lease was never acquired.

## Dependencies

- `agent/turn_liveness_registry.py` — new, no dependencies beyond stdlib.
- `agent/turn_facade_lease.py` — imports the registry.
- `hermes_state.py` — imports the registry lazily inside the predicate, so the
  low-level state module gains no hard dependency on the agent package.
- `gateway/lifecycle_ledger.py` — stdlib only (`threading`, `inspect`).
- `hermes_state_compression.py` — unchanged; it already imports the reclaim
  predicate from `hermes_state`, which is the seam the turn-token check
  attaches to.

## See Also

- `Homelab/50-Agent-Memory/Ultron/2026-11-16-gateway-f2-f4-not-on-live-tree.md`
  — Ultron's findings card: the verified absences, the divergence measurement,
  and the log evidence that dates the branch switch.
- `Homelab/50-Agent-Memory/Friday/2026-11-16-f2-f4-live-port.md` — this
  card's handoff.
- `~/.hermes/skills/workflow/agent-gateway-recovery/references/gateway-self-wedge-recovery.md`
  — the tested recovery runbook (stale-lease reclaim, restart caveats).
- `docs/wiki/services/duplicate-tool-call-ids.md` — the earlier live-branch
  port that established the pattern this one follows.
