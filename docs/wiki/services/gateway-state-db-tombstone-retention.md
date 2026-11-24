# Gateway state.db Terminal/Admission Tombstone Retention

The gateway's terminal/admission tombstone namespaces in `state.db` grew without
bound because the reclaim machinery existed and worked but was **never called
from production**. This page documents the fix: a 7-day retention sweep for
terminal evidence, and the fence/evidence distinction that makes it safe.

Written 2026-11-24, landed 2026-11-24 (Hermes commit
`6cb61370e3` on `fix/state-db-tombstone-retention-t_e4622e38`, kanban
`t_e4622e38`). Obsidian mirror:
`Homelab/10-Services/Services/gateway-state-db-tombstone-retention.md` — the vault
copy is the canonical published page and carries the same content.

## Overview

### The growth

Measured read-only against the live ultron profile's `state.db` (1.83 GB):

| Namespace | Rows | Payload |
|---|---|---|
| `gateway.terminal_worker.v1.*` | 10,650 | 63.2 MB |
| `gateway.terminal_admission.v1.*` | 10,679 | — |
| `gateway.admission.result.v1.*` | 10,902 | — |
| `gateway.retired_session.v1.*` | 11,561 | — |

Every other `DELETE FROM state_meta` call site targets a specific key or a
specific namespace. Nothing swept these three namespaces generically, so they
grew without limit.

The bulk was one 11-second burst on 2026-11-18: a worker-session orphan reap
retiring 245 orphaned worker sessions. Each `terminal_worker` tombstone carries
a copy of the `worker_receipts` rows — **average 49 receipts, maximum 1,161** —
so one retired session can pin kilobytes of receipt data in `state_meta`
indefinitely.

`prune_sessions` does **not** help: it deletes `sessions`/`messages` rows only
and never touches `state_meta`.

### The actual bug

The reclaim machinery already existed:

```python
hermes_state_mutation_retirement.prune_retired_tombstones(conn, older_than=RETRY_HORIZON_S)
RETRY_HORIZON_S = 604800  # 7 days
```

It was referenced only by tests. Production never called it. The prune existed,
worked, and was never invoked.

### Fence vs evidence — the part that is easy to get wrong

There are two different objects with **opposite** retention rules:

```
FENCE    = gateway.retired_session.v1.<id>
           The deletion fence. A user delete writes this marker WITHOUT an
           "expires" key. It must survive FOREVER: without it a deleted
           session id could be recreated, re-admitted, or reissued as a Bot
           Chat generation. It is what these actually consult:
             gateway/session_api.py       (re-bind)
             hermes_state_mutations.py   (re-admit)
             hermes_state_terminal.py    (exact retry)
           The terminal tombstones are NOT consulted by any of those three.

EVIDENCE = gateway.terminal_admission.v1.<id>
           gateway.terminal_worker.v1.<id>
           gateway.admission.result.v1.<id>
           Proof that a request settled. It ages out at the 7-day retry
           horizon, whoever retired the session — user delete or housekeeping
           sweep.
```

The fence and the evidence have **independent lifetimes**. Do not couple them.

The fence's own protection lives in `_reclaim_expired()`, which **does** require
`json_extract(value,'$.expires')`. That is the mechanism that keeps a user
delete's marker alive forever — `keep_passed_fences()` explicitly *removes*
`$.expires` from it (`hermes_state_mutation_retirement.py`), so it is
permanently unreclaimable.

Note that the worker-tombstone delete path deliberately does **not** require
`"expires"`. That is correct, not a bug: terminal evidence ages out for
everyone.

### The bot-receipt contract a coupled design would break

An earlier attempt required the terminal tombstone to carry `"expires"`. It broke
7 tests in `tests/gateway/test_bot_receipt_tombstone_expiry.py`.

The bot-receipt path wants a user-deleted chat's terminal proof to **age out**
so the receipt keeps its OWN recorded answer (`gateway/session_bot.py`
`_expired_outcome`). Keeping the proof alive instead made `_admission_outcome`
read `outcome='completed'` and resolve the receipt as "settled" — the wrong
answer, from the bot's point of view.

## Architecture

### The sweep

```
hermes_state_mutation_retirement.py
  prune_retired_tombstones(conn, older_than=RETRY_HORIZON_S)
    1. stamp retired_at=now on undated ADMISSION / WORKER tombstones
    2. stamp retired_at=now on undated RESULT rows           <-- new
    3. for each aged ADMISSION tombstone: delete it + its result row
       + its identity pointer (the triple-delete)
    4. delete aged WORKER tombstones
    5. delete aged ORPHANED result rows                     <-- new
    6. _reclaim_expired(RETIRED_PREFIX)  -> only rows WITH 'expires'
    7. _reclaim_expired(ROUTE_FENCE_PREFIX)
```

Step 2 exists because a result row is otherwise **undated**, so an age-based
reclaim can never reach it. Measured: every one of the 10,902 live result rows
had `retired_at IS NULL`.

Step 5 exists because the triple-delete in step 3 is keyed on the **parent's**
`admission_id`. Once the admission tombstone is gone the result row is an
**orphan** with no parent to key on, so the triple-delete can never reach it.
Measured on the live store: 295 such rows.

```sql
DELETE FROM state_meta
WHERE key GLOB 'gateway.admission.result.v1.*'
  AND json_valid(value)
  AND json_extract(value, '$.retired_at') < :cutoff
  AND NOT EXISTS (
    SELECT 1 FROM state_meta a
    WHERE a.key = 'gateway.terminal_admission.v1.' ||
          substr(state_meta.key, length('gateway.admission.result.v1.') + 1))
```

Both sides of the subquery use the `state_meta` primary key, so it is an index
lookup, not a table scan. Verified with `EXPLAIN QUERY PLAN`:

```
SEARCH state_meta USING INDEX sqlite_autoindex_state_meta_1 (key>? AND key<?)
CORRELATED SCALAR SUBQUERY 1
SEARCH a USING COVERING INDEX sqlite_autoindex_state_meta_1 (key=?)
```

### The maintenance entry point

```
hermes_state_maintenance.py
  SessionMaintenanceMixin.prune_retired_tombstones(older_than_seconds=None) -> int
    -> hermes_state_mutation_retirement.prune_retired_tombstones(conn, older_than=...)
    -> returns the number of state_meta rows reclaimed
       (count before minus count after, over the three terminal prefixes)
```

It **never VACUUMs**: a prune does not shrink the file, and a VACUUM needs
exclusive access plus free space to rewrite the whole store. The default horizon
is `RETRY_HORIZON_S` (7 days).

### The call site

```
gateway/run.py
  _housekeeping_state_db_maintenance()
    -> _adb.prune_retired_tombstones()          <-- FIRST, before the
                                                    auto_archive/auto_prune gate
    -> then the existing archive / prune / vacuum logic
```

The sweep is deliberately **NOT** gated on `sessions.auto_prune`: the tombstones
are gateway admission proofs, not session history, so a profile that never
prunes sessions still leaks them. It is wrapped in try/except and logged at
warning on failure, so a sweep error can never take the gateway down.

The existing callers already make this per-profile, which is what is needed:

| Caller | When |
|---|---|
| `gateway/run_runtime_init.py:199` | startup maintenance, via `_for_each_served_profile` |
| `gateway/run.py:4502` | the recurring "state.db maintenance tick" (every 60 s), via `profile_scoped_chore` |

Both resolve `acquire()` / `get_hermes_home()` / `load_config()` through the
active profile scope, so the ultron gateway sweeps **ultron's** `state.db`.

## Operation

### Verify the fix is on the branch the gateway executes

```bash
cd /home/serveradmin/.hermes/hermes-agent
git rev-parse --abbrev-ref HEAD                        # canonical-deploy-live
git grep -c 'prune_retired_tombstones' -- hermes_state_maintenance.py
git grep -c 'prune_retired_tombstones' -- gateway/run.py
git cat-file -e HEAD:hermes_state_mutation_retirement.py && echo PRESENT
```

**A fix that is green on a branch the gateway does not execute is not a fix.**

### Watch the sweep

```bash
grep -E 'tombstone sweep' ~/.hermes/logs/gateway.log
```

A successful sweep logs `state.db tombstone sweep reclaimed N gateway
tombstone row(s)` at INFO when N > 0. A failure logs
`state.db tombstone sweep skipped: ...` at WARNING — the sweep failing is not
fatal, but it does mean the namespace is still growing.

### Measure the namespaces

```bash
sqlite3 ~/.hermes/profiles/<profile>/state.db "
  SELECT
    CASE
      WHEN key LIKE 'gateway.terminal_worker.v1.%'     THEN 'worker'
      WHEN key LIKE 'gateway.terminal_admission.v1.%'  THEN 'admission'
      WHEN key LIKE 'gateway.admission.result.v1.%'     THEN 'result'
      WHEN key LIKE 'gateway.retired_session.v1.%'      THEN 'fence'
    END AS ns,
    count(*) AS rows
  FROM state_meta
  WHERE key LIKE 'gateway.%'
  GROUP BY ns ORDER BY rows DESC;"
```

### Run the regression tests

```bash
cd <worktree>
scripts/run_tests.sh tests/gateway/test_state_db_tombstone_prune.py
```

## Configuration

There is **no new configuration.** The horizon is the existing
`RETRY_HORIZON_S` constant (604800 s, 7 days) — the same exact-retry window the
gateway already honours.

| Setting | Default | Meaning |
|---|---|---|
| `RETRY_HORIZON_S` | `604800` s (7 d) | Age at which terminal evidence ages out. Shared with the exact-retry window. |
| `sessions.auto_prune` | unset | **Deliberately NOT consulted** by the tombstone sweep. |

## Troubleshooting

### Symptom: `state.db` keeps growing

Check the namespaces (the command under Operation). If the worker/admission/result
counts are climbing, the sweep is not running. Confirm the fix is on the branch
the gateway executes, and check the gateway log for `tombstone sweep skipped`.

### Symptom: the sweep ran but nothing was reclaimed

Expected on the **first** pass. Pre-existing undated rows are stamped with
`retired_at = now` and age from that pass, so they become reclaimable only
after the horizon elapses. On the live ultron store the stamp landed
2026-11-24 and the backlog became eligible on **2026-12-01**.

Check the stamp landed:

```bash
sqlite3 ~/.hermes/profiles/<profile>/state.db "
  SELECT count(*) FROM state_meta
  WHERE key LIKE 'gateway.admission.result.v1.%'
    AND json_extract(value,'\$.retired_at') IS NULL;"
```

A non-zero count means the stamp has not run yet. Zero means it has, and the
rows are simply inside the horizon.

### Symptom: the store did not shrink after the sweep

Expected. **A prune does not shrink the file.** The rows are gone, but SQLite
keeps the freed pages for reuse. The file only shrinks on VACUUM, which needs
exclusive access plus free space to rewrite the whole store. The sweep
deliberately never VACUUMs.

### Symptom: a deleted session id came back

That would be a fence failure and is a serious bug. The fence
(`gateway.retired_session.v1.<id>`) must survive forever. Check:

```bash
sqlite3 ~/.hermes/profiles/<profile>/state.db "
  SELECT key, json_extract(value,'\$.expires'), json_extract(value,'\$.retired_at')
  FROM state_meta WHERE key LIKE 'gateway.retired_session.v1.%' LIMIT 20;"
```

A user delete's marker has **`expires` = NULL** (absent). A housekeeping
sweep's marker has `expires` = 1 and may age out — that is correct and
different.

On the live ultron store at the time of writing, all 11,561 `retired_session`
markers carry `expires` (i.e. every one of them is a housekeeping-sweep
marker) and none is a user-delete fence. A profile that only ever sweeps will
therefore show `NO expires (delete fence) = 0`, and that is expected — it
means no session was deleted by a user on that profile.

### Symptom: the bot receipt for a deleted chat returned the wrong answer

The bot-receipt path needs a user-deleted chat's terminal proof to age out so
the receipt keeps its OWN recorded answer. If a proof is being kept alive
because it carries `expires`, the receipt will resolve as "settled" instead.
That is the coupling bug the design deliberately avoids — see "The bot-receipt
contract a coupled design would break" above.

## Dependencies

- `hermes_state_mutation_retirement.py` — the sweep itself; stamps `retired_at`
  on undated result rows and adds the orphaned-result reclaim.
- `hermes_state_maintenance.py` — `SessionDB.prune_retired_tombstones()`, the
  maintenance entry point. Never VACUUMs.
- `gateway/run.py` — `_housekeeping_state_db_maintenance()`, the call site.
  Profile-scoped through the existing callers.
- `hermes_state_terminal.py` — owns the `ADMISSION_PREFIX` / `WORKER_PREFIX` /
  `RESULT_PREFIX` constants and the `identity_key()` used by the triple-delete.
- `gateway/session_api.py`, `hermes_state_mutations.py`, `hermes_state_terminal.py`
  — the fence consumers. Unchanged by this fix; they read the
  `RETIRED_PREFIX` marker, which this fix does not touch.

## Tests

- `tests/gateway/test_state_db_tombstone_prune.py` — **new**, 9 tests. Covers:
  the aged worker tombstone; the aged admission tombstone with its result row;
  the orphaned result row; the undated result row stamp; a user delete's fence
  surviving the sweep; a swept session's own marker ageing out; a tombstone
  inside the horizon surviving; live sessions/messages left alone; the sweep
  reporting what it reclaimed.
- `tests/gateway/test_terminal_commit_crash.py` — one assertion **narrowed**.
  It asserted `snapshot()[1] == saved`, comparing the whole stored result
  ENVELOPE across a gateway restart. The envelope now legitimately gains a
  `retired_at` field, so the assertion compares the result PAYLOAD
  (`snapshot()[1]['result'] == saved['result']`) — which is what the test's
  intent (the exact result survives the crash) actually requires.
- `tests/gateway/test_session_mutation_retirement.py` — 2 passed.

### Pre-existing failures — NOT caused by this change

These fail identically on the unmodified base branch. They are environment /
test-hygiene failures: the same "file I/O against the REAL hermes home"
assertion from `tests/home_io_guard.py`, tripped because the suite was run
inside a git worktree.

```
tests/gateway/test_local_closed_retirement.py    9 failed
tests/hermes_state/test_terminal_retirement.py    6 failed
tests/gateway/test_settled_result_size.py        1 failed
tests/gateway/test_retire_settles_waiters.py     1 failed
                                                   ------
                                                   17 failed, 15 passed
```

Measured on base `2e8e67775d` and on the change branch: **identical**
`17 failed, 15 passed`. Do not try to fix these in this change.

## See Also

- `docs/wiki/services/gateway-state-db-integrity-bounds-and-turn-lease-reclaim.md`
  — the other state.db page: the bounded integrity check and the stale
  turn-lease reclaim. Different faults, same store.
- `docs/wiki/services/hermes-compaction-fleet-ceiling.md` — the other bound
  on the same store's growth.
- `Homelab/50-Agent-Memory/Ultron/2026-11-24-tombstone-retention-diagnosis.md`
  — the diagnosis card: the live-store measurements this fix is based on.
