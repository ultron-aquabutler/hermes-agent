# Kanban Auxiliary Fallback Chain and the Bounded `blocker_auth` Cooldown

Two defects kept the Kanban dispatcher from spawning workers for ~2h45m on
2026-11-23 while the ready queue was non-empty:

1. the auxiliary (non-main) client **refused to use the operator's declared
   `fallback_providers` chain** for an explicit-provider task, and
2. the respawn guard's `blocker_auth` reason **parked indefinitely**, with no
   time bound at all.

Either one alone would have been survivable. Together they were fatal: the
declared fallback was never tried, and the card that recorded the failure was
never released.

Written 2026-11-23 by @friday, for kanban `t_498b52ce`.

## Overview

### The incident

On 2026-11-23 the friday gateway's kanban dispatcher stopped spawning workers
from 12:49 to 15:01 — roughly **2h45m** — while the ready queue stayed
non-empty. The gateway detected the stall and logged it as a WARNING only:

```
12:49:41  kanban dispatcher stuck: ready queue non-empty for 10 consecutive ticks but 0 workers spawned. Last tick held back: blocker_auth=***
13:28:22  ... 10 consecutive ticks ... blocker_auth=***
14:50:38  ... 26 consecutive ticks ... blocker_auth=***
15:01:31  ... 36 consecutive ticks ... blocker_auth=***
```

The "consecutive ticks" counter climbing 10 → 26 → 36 with the queue still
full is the signature of a *persistent* blocker, not a blip.

### The proximate cause

The main model provider (**minimax**) was unavailable. The gateway said so
directly, interleaved with the same log window:

```
Auxiliary kanban_decomposer: main provider minimax is unavailable and no
fallback_chain / fallback_providers is configured — refusing to guess another
logged-in provider. Re-authenticate (`hermes model`) or declare a fallback.
```

That message appeared **108 times** between 13:51 and 14:59, and **0 times**
after the 15:29:47 gateway restart.

### The key was NOT spelled wrong

The message claims no `fallback_providers` is configured. The friday profile
config plainly declared one:

```yaml
fallback_providers:
  - provider: copilot
    model: gpt-6-luna
    base_url: https://api.individual.githubcopilot.com
```

This is the diagnostic trap in the original card: the wording sends triage
looking for a key-name mismatch (`fallback_chain` vs `fallback_providers`)
when the real cause is that **the ladder never consulted the declared chain
at all**.

### Why the ladder refused

The auxiliary recovery ladder has two distinct notions of "another provider":

* the **DECLARED** chain — `fallback_providers` in the profile config, plus the
  legacy `fallback_model`. The operator pointed this profile at these
  providers, so using one is not a guess.
* the **DISCOVERY** chain — whatever else happens to be logged in on the host.
  Using one bills an account the operator never pointed this session at, and
  the ladder refuses it by design.

Before this fix the ladder only reached the DECLARED chain when the task's
provider was `auto`. For an **explicit-provider** task (friday's
`kanban_decomposer` → `custom:colibri`), an auth-flavoured failure returned
early unless the *task* had its own `fallback_chain`:

```python
explicit_auth_with_task_chain = (
    reason == aux_errors.AUTH_ERROR and not is_auto and has_task_fallback_chain
)
```

So: main provider down → auth-flavoured error → explicit provider, no task
chain → the ladder returned `None` → the spawn was recorded as `blocker_auth`
→ the respawn guard parked it **forever**.

### Why it stayed parked forever

`check_respawn_guard()` had time bounds for the quota and rate-limit families
but **none** for `blocker_auth`. A provider outage therefore latched the whole
ready queue until a human intervened.

This bound had existed before. It was introduced on the
`fix/blocker-auth-outcome-based-t_258dc2c3` line (`165954667c`, with a 24-hour
default) and **lost** when `342ff1ce1f` restored the respawn-guard quota/auth
split onto the live dispatch path — so the live tree parked on auth forever
again.

## Architecture

| Piece | Location | Role |
|---|---|---|
| `_declared_main_fallback_chain()` | `agent/auxiliary_client.py` | Reads the operator's DECLARED chain (`fallback_providers` + legacy `fallback_model`) via `get_fallback_chain(load_config_readonly())` |
| `_ladder_provider_fallback()` | `agent/auxiliary_client.py` | The fallback ladder. Now reaches the DECLARED chain for an explicit-provider auth failure |
| `_next_fallback_after_quarantine()` | `agent/auxiliary_client.py` | Re-walk after a candidate is quarantined; `allow_declared_main_chain` permits the DECLARED chain on that re-walk |
| `_discovery_chain_allowed()` | `agent/auxiliary_client.py` | Still **refuses** the DISCOVERY chain. Its warning now distinguishes "nothing declared" from "declared but exhausted" |
| `DEFAULT_AUTH_BLOCK_SECONDS` | `hermes_cli/kanban_db_dispatch.py` | Bounded cooldown for `blocker_auth` (1800 s) |
| `_resolve_auth_block_seconds()` | `hermes_cli/kanban_db.py` | Reads `HERMES_KANBAN_AUTH_BLOCK_SECONDS` |
| `check_respawn_guard()` | `hermes_cli/kanban_db_dispatch.py` | Applies the cooldown: parks inside the window, re-probes after it |

### The boundary that is deliberately preserved

Two things did **not** change, and both are load-bearing:

* **The DISCOVERY chain is still refused.** An explicit-provider task never
  falls through to "whatever else is logged in". The fix only widens the
  ladder to the chain the operator *declared*.
* **The main-agent-model rung is still excluded** for an explicit pin. The
  main session's own model is not a fallback candidate for a task that
  deliberately pinned a different provider (`#t_e94832a1` context-size leak).

So the ladder gained exactly one new capability: an explicit-provider task may
now use the DECLARED `fallback_providers` chain. Nothing else moved.

### The cooldown anchors on the run that stamped the failure

The cooldown is keyed to the `ended_at` of the **latest ended run** — the run
whose end-of-run transaction wrote `last_failure_error`. If there is **no**
ended run (a legacy row, or a hand-written/imported `last_failure_error`),
there is nothing to date the cooldown against, and an undated auth stamp is
indistinguishable from a dead credential — so it **keeps parking**, exactly as
before this bound existed. Returning `None` there would turn a genuine 401 into
a hot retry loop.

The separate `consecutive_failures` breaker remains the durable safety net: a
credential that is genuinely dead still ends up parked for a human. This bound
only stops a single provider outage from latching forever.

## Operation

Both behaviours are automatic. No operator action is required.

To watch for the stall signature in the gateway log:

```bash
journalctl --user -u hermes-gateway --since "1 hour ago" \
  | grep "dispatcher stuck"
```

A healthy board shows `0 workers spawned` never appearing with a non-empty
ready queue. If it appears, read the `Last tick held back:` reason:

| Held-back reason | Meaning | Self-heals? |
|---|---|---|
| `blocker_auth` | Auth-flavoured failure text on the card | Yes — after `DEFAULT_AUTH_BLOCK_SECONDS` |
| `quota_cooldown` | Quota/credits wall | Yes — after `DEFAULT_QUOTA_BLOCK_SECONDS` |
| `rate_limit_cooldown` | Clean rate-limited requeue | Yes — after the rate-limit cooldown |
| `recent_success` | Card already succeeded recently | No — deliberate |
| `active_pr` | A PR URL is in a recent comment | No — deliberate |

To check whether a card is parked under the auth guard and how long it has
been:

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python3 -c "
import sqlite3, sys
sys.path.insert(0, '.')
from hermes_cli import kanban_db_dispatch as kd
c = sqlite3.connect('file:/home/serveradmin/.hermes/kanban/boards/homelab/kanban.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
tid = '<task_id>'
print('guard   :', kd.check_respawn_guard(c, tid))
print('cooldown:', kd.DEFAULT_AUTH_BLOCK_SECONDS, 'seconds')
"
```

Note the `c.row_factory = sqlite3.Row` line — the guard reads `row[...]`
keys, so a plain tuple row raises `TypeError`.

To confirm the auxiliary ladder sees a declared chain on this profile:

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
from agent.auxiliary_client import _declared_main_fallback_chain
print(_declared_main_fallback_chain())
"
```

An empty list means nothing is declared, and an explicit-provider task will
still refuse — correctly.

### The dispatcher needs a restart to pick up code changes

The dispatcher and the auxiliary client live in the gateway process. A code
change does not take effect until the gateway restarts:

```bash
systemctl --user show hermes-gateway.service -p ActiveState
```

## Configuration

| Knob | Default | Where |
|---|---|---|
| `DEFAULT_AUTH_BLOCK_SECONDS` | `1800` (30 minutes) | `hermes_cli/kanban_db_dispatch.py` |
| `HERMES_KANBAN_AUTH_BLOCK_SECONDS` | unset → uses the default | environment |
| `HERMES_KANBAN_AUTH_BLOCK_SECONDS=0` | disables the cooldown (re-probe next tick) | environment |
| `fallback_providers` | per-profile list | `~/.hermes/profiles/<profile>/config.yaml` |

`HERMES_KANBAN_AUTH_BLOCK_SECONDS=0` matches the quota and rate-limit escape
hatches: the card respawns on the very next tick instead of entering the
parked state.

30 minutes is deliberately short relative to the outage it has to cover. A
provider outage should re-probe on the order of a dispatcher tick cycle, not on
the order of a human's lunch break. The historical (lost) implementation used
24 hours — a 24-hour park of a whole ready queue is the exact failure mode
this page is about.

## Troubleshooting

### The dispatcher is stuck and the log says `blocker_auth`

The card carries auth-flavoured failure text. After the cooldown it re-probes
on its own. If it does not, check that the running gateway actually loaded the
fix:

```bash
cd /home/serveradmin/.hermes/hermes-agent
git log -1 --format='%h %s'
systemctl --user show hermes-gateway.service -p ActiveState
```

### The auxiliary refused and the message mentions `fallback_providers`

Read the message carefully — the wording is now diagnostic:

* *"the built-in discovery chain is disabled for an install with a selected main
  provider … The DECLARED fallback_providers chain was already tried and did
  not yield a usable client"* → a fallback **is** declared, but every entry in
  it failed to produce a usable client (expired key, no credentials, unhealthy
  endpoint). Re-authenticate or extend the chain.
* *"no fallback_chain / fallback_providers is configured"* → genuinely nothing
  is declared for this profile. Declare one.

### The auxiliary refused and you expected the declared chain to be used

Check that the task is not pinned in a way that excludes the chain, and that
the declared entry can actually build a client:

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
from agent.auxiliary_client import _declared_main_fallback_chain
for e in _declared_main_fallback_chain():
    print(e)
"
```

An entry with no usable credentials is skipped silently (it is a *declared*
route, so no warning is logged for each skip — the exhaustion warning at the
end names what was tried).

### A card is parked `blocked` with an auth error

That is the breaker, not this cooldown. The cooldown only governs the
*parked-under-guard* state; the breaker is what moves a card to `blocked` after
repeated failures. See the `consecutive_failures` column and the rate-limit
page for the sibling path.

### A card never re-probes at all

Check the cooldown is not set absurdly high, and that there is an ended run to
anchor it. A card whose `last_failure_error` was set by hand (not by a run)
keeps parking by design — there is no timestamp to date the cooldown against:

```bash
sqlite3 -header -column \
  "file:/home/serveradmin/.hermes/kanban/boards/homelab/kanban.db?mode=ro" \
  "SELECT id, status, consecutive_failures, last_failure_error
     FROM tasks WHERE id = '<task_id>';"
```

## Dependencies

- `agent/auxiliary_client.py` — the auxiliary recovery ladder and the
  DECLARED-vs-DISCOVERY chain distinction.
- `agent/auxiliary_error_policy.py` — `Failure` / `ClassifiedError`, the
  classification that decides "auth error" vs "capacity error".
- `hermes_cli/fallback_config.py` — `get_fallback_chain()`, the single reader
  of `fallback_providers` and the legacy `fallback_model`.
- `hermes_cli/kanban_db_dispatch.py` — the respawn guard and the bounded
  cooldown.
- `hermes_cli/kanban_db.py` — `_resolve_auth_block_seconds()`, alongside the
  quota and rate-limit cooldown resolvers.
- `tests/agent/test_auxiliary_declared_fallback_chain.py` — the ladder
  behaviour.
- `tests/hermes_cli/test_kanban_auth_block_cooldown.py` — the cooldown
  behaviour.

## See Also

- `docs/wiki/services/kanban-rate-limit-retry-budget.md` — the sibling bound
  on the same dispatcher: an attempt-count budget for the quota-wall path.
- `docs/wiki/services/aux-fallback-context-screen.md` — how fallback
  candidates are screened by context size before being forwarded.
- `docs/wiki/services/deploy-branch-guard.md` — the other dispatcher guard that
  can stop a board from dispatching.

## Change History

| Date | Change | Commit |
|---|---|---|
| 2026-11-23 | Auxiliary ladder now honours the DECLARED `fallback_providers` chain for an explicit-provider auth failure, and the refusal message distinguishes "nothing declared" from "declared but exhausted". `blocker_auth` given a bounded cooldown (`DEFAULT_AUTH_BLOCK_SECONDS`, 30 min) so a provider outage cannot park the ready queue forever. Fixes the 2026-11-23 minimax outage (0 workers spawned for ~2h45m, 12:49→15:01). | `t_498b52ce` |
