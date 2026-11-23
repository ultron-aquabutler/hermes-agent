# Hermes Compaction Fleet Ceiling & Runaway Session Guard

Two layered guards on model-context spend in the Hermes gateway:

| Layer | What it does | Where |
|---|---|---|
| **Fleet ceiling** | The compaction trigger can never exceed 120,000 tokens, whatever any config says | `agent/compression_fleet_ceiling.py` |
| **Runaway guard** | A routed session whose live transcript stays huge AND long-lived is rotated to a fresh context | `gateway/runaway_session_guard.py` |

The ceiling bounds *when compaction fires*. The guard bounds *one route's lifetime cost* when
compaction alone cannot help — a transcript that stays large is re-read from the prompt cache on
every single call, no matter how often you compact it.

Written 2026-11-16, landed 2026-11-23 (Hermes commit on `canonical-deploy-live`, kanban
`t_4770e5b3`; originating card `t_6abebfd8`). Obsidian mirror:
`Homelab/10-Services/Services/hermes-compaction-fleet-ceiling.md` — the vault copy is the
canonical published page and carries the same content.

> **Status note (2026-11-23).** The code is **landed in the tree** on `canonical-deploy-live`,
> but the **live gateway has not been restarted**, so the running process does not yet import
> `agent/compression_fleet_ceiling.py` or `gateway/runaway_session_guard.py`. A gateway restart
> requires Bryan's explicit approval and must not be performed from inside the gateway's own
> process tree.
>
> **The config value and the effective trigger are not the same thing.** All 8 profile homes
> carry `compression.threshold_tokens: 300000` — a temporary fleet-wide mitigation written
> 2026-11-18 14:02Z while the MiniMax plan cap was exhausted. The 120,000 ceiling is enforced
> **in code**, so once this code is running the effective trigger on every home is **120,000**
> regardless of the 300000 config value. Measured 2026-11-23, resolver -> real
> `ContextCompressor` -> effective cap: every one of the 8 homes resolves to 120000.

## Overview

Fleet token spend was dominated by **cache-read** on long Kanban sessions that never compacted.
On 2026-11-11 the fleet was spending ~$80-87/day, ~84% of it cache-read, because the compaction
trigger sat far above the context any long session actually reached.

The root cause: a 1M-token window (MiniMax-M3) with the ratio trigger at 0.5 puts the trigger at
~500,000 tokens. Measured over 2026-11-11, 100% of sampled long Kanban sessions had **zero**
compaction events — their observed per-call context peaked at 130k-168k tokens and never reached
the 500k trigger. Every subsequent API call then re-read the entire growing context from the
prompt cache.

A config-level fix (`compression.threshold_tokens`) stopped the burn, but a config value is only
as durable as the config file: a `config reset`, a new profile, a config migration, or a
hand-edited YAML reopens the hole. These guards make the ceiling a property of the **code**.

## Architecture

### The fleet ceiling

`ContextCompressor._effective_threshold_cap(context_length)` clamps the effective trigger to
`min(configured_cap, context_length, FLEET_COMPRESSION_CEILING_TOKENS)`. Every compressor — the
main agent, a delegated child, a compressor rebuilt after a model switch — resolves its trigger
through that one method, so the ceiling holds on every path.

The contract: **config may only tighten the trigger below the ceiling, never raise it above.**

```
compression.threshold_tokens = 60000   -> trigger 60,000   (tighter: honoured)
compression.threshold_tokens = 500000  -> trigger 120,000  (raised: clamped)
compression.threshold_tokens unset     -> trigger 120,000  (ceiling still applies)
```

`agent/compression_fleet_ceiling.py` also exports `clamp_to_fleet_ceiling()` for callers that want
the same clamp outside the compressor.

### Why the ceiling is in CODE and not only in config

Profile configs **do not inherit the root config** — by design. `hermes_cli/config.py` does
`config = deepcopy(DEFAULT_CONFIG)` and then deep-merges the *profile's own* `config.yaml`; the
root `config.yaml` is never consulted when a profile is active. (`hermes_cli/AGENTS.md`: "no live
config inheritance — profiles are independent islands by design.") So `DEFAULT_CONFIG` is the only
lever that reaches every profile, present and future — and it still is only a config value.

The concrete proof that config alone is not durable: `hermes_cli/config_migrations.py` carries a
46 -> 47 migration that rewrites `compression.threshold_tokens` from the old `256000` default back
to `null` (ratio-only). Any migration, `doctor --fix`, a `config reset`, or a hand-edited YAML can
reopen the hole. The code ceiling is immune to all of them.

### The runaway guard

`gateway/session_lifecycle.py::_route_reset_reason` consults the guard and returns the reset
reason `runaway_limit` when a route's live context segment has run away. It sits beside the
existing `"suspended"` reason and does not change it.

The invariant *"only explicit suspension replaces a routed conversation; time never does"* still
holds for ordinary age. The runaway guard is a **separate, explicitly-configured rule** with its
own reset reason — not a reinterpretation of age.

Every failure path in the guard returns "not tripped": a config read error, a missing DB
resolver, or a failed SQL lookup never rotates a live conversation.

## Configuration

```yaml
compression:
  threshold_tokens: 120000           # fleet ceiling; config may only lower it
  hygiene_hard_message_limit: 400    # "no token data" backstop (was 5000)
  runaway_max_segment_hours: 336     # 14 days; 0 disables this half
  runaway_max_live_chars: 800000     # ~200K tokens of transcript; 0 disables this half
  runaway_max_session_messages: 5000 # backstop; 0 disables
  proactive_prune_tokens: 0          # stays OFF (measured summary-failure risk)
```

All of these are `DEFAULT_CONFIG` values in `hermes_cli/config_defaults.py`; a profile's own
`config.yaml` may override them. The new keys deep-merge automatically — no config version bump
is required.

The size and age limits are **ANDed**: a segment trips only when it is BOTH oversized AND
long-lived. Either half alone is a bad rule (see Troubleshooting). The message-count backstop
stands alone, because a transcript with tens of thousands of rows is pathological whatever its
age or size.

## Operation

### Verifying the ceiling is live

```bash
# Through hermes' own resolver, per profile
hermes --profile friday config get compression.threshold_tokens
```

Then confirm the **effective** cap, not the config value — the config can say 300000 while the
effective trigger is 120000:

```python
from hermes_cli.config import load_config
from agent.context_compressor import ContextCompressor

comp = load_config()["compression"]
c = ContextCompressor(model="MiniMax-M3", threshold_tokens_cap=comp.get("threshold_tokens"))
c._effective_threshold_cap(1_000_000)   # -> 120000
```

Per-home state as measured on 2026-11-23 (all 8 homes, resolver -> real `ContextCompressor` ->
effective cap):

| Home | config `threshold_tokens` | effective cap (1M window) | effective cap (200k) | effective cap (64k) |
|---|---|---|---|---|
| root | 300000 | **120000** | 120000 | 64000 |
| default | 300000 | **120000** | 120000 | 64000 |
| curator | 300000 | **120000** | 120000 | 64000 |
| friday | 300000 | **120000** | 120000 | 64000 |
| hermes-gateway | 300000 | **120000** | 120000 | 64000 |
| librarian | 300000 | **120000** | 120000 | 64000 |
| shuri | 300000 | **120000** | 120000 | 64000 |
| ultron | 300000 | **120000** | 120000 | 64000 |

The `300000` config values are a **temporary fleet-wide mitigation** written on 2026-11-18 14:02Z
while the MiniMax plan cap was exhausted; it throttled compaction frequency to reduce
aux-summary calls during the quota wall. The wall is lifted (provider serving 200s since
2026-11-19T00:21Z), so the mitigation's premise is gone. Because the ceiling is code, the
effective trigger is already 120,000 on every home **regardless** of that config value — the
mitigation no longer has any effect once this code is running.

`runaway_max_segment_hours` (336), `runaway_max_live_chars` (800000) and
`runaway_max_session_messages` (5000) resolve from `DEFAULT_CONFIG` on every home even though no
profile `config.yaml` sets them — `load_config()` merges the defaults, so the guard sees them.

### Verifying the guard

The guard trips on the **live transcript** — the rows with `messages.active = 1`:

- **segment age** = `MIN(timestamp)` over active rows
- **live size** = `SUM(LENGTH(content))` over active rows

```sql
SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0),
       MIN(timestamp)
FROM messages WHERE session_id = ? AND active = 1;
```

To see what the shipped limits would trip on the live fleet:

```python
from gateway.runaway_session_guard import RunawayLimits, check_route_runaway
# per live profile state.db, per session id
```

### Reading the guard's log line

```
runaway_session_guard: route agent:main:telegram:dm:8943042074 (session 20261024_215722_f17a4d46)
tripped: live transcript 803712 chars exceeds limit 800000 and segment age 544.6h exceeds
limit 336h — rotating to a fresh session
```

## Troubleshooting

### The guard is not tripping when a session looks huge

Check whether it is the **conversation** or the **segment** that is huge. `sessions.started_at` is
the conversation's start. Under the default `compression.in_place: true` the session id does NOT
change on compaction and `started_at` is never rewritten:

- `hermes_cli/config_defaults.py` ships `compression.in_place: True`.
- `hermes_state_mutation_compress.py` — in-place path sets `child = target` (same session id) and
  only bumps `runtime_generation`; `started_at` is untouched.
- `hermes_state_messages.py::_archive_and_compact_on_conn` sets `active = 0` on archived rows and
  **inserts** the compacted summary as new rows.

So a session can be 544h old as a conversation while its live segment is only 110h old and 242
rows. The guard measures the segment, deliberately.

### The guard rotated a session I wanted to keep

It fires only when the transcript is over `runaway_max_live_chars` AND the segment is over
`runaway_max_segment_hours`. Check the live numbers before assuming it was a false positive:

```sql
SELECT COUNT(*) rows, COALESCE(SUM(LENGTH(content)),0) chars, MIN(timestamp) oldest
FROM messages WHERE session_id = '<id>' AND active = 1;
```

Raise `runaway_max_live_chars` (or set it to 0 to disable the size half) if the route is
legitimately huge and still in use. Note that a **large but young** segment never trips — only age
AND size together do.

### Compaction fires but the session still reads a lot of cache

Expected. Compaction bounds the trigger; it cannot shrink the *fixed* per-call overhead (system
prompt + tool schemas). Measured on the live fleet: friday's persisted system prompt is 42,683
chars (~10.7k tokens) and the serialized tool definitions are ~11.5k tokens. The healthy
short-session floor is ~120,604 cache-read tokens per call — that floor is not a compaction
problem.

### I set `threshold_tokens` higher and nothing changed

That is the ceiling working. Config may only tighten the trigger. Lowering it works; raising it
above 120,000 is clamped.

### The guard did not trip and should have

Check that the route's *live* transcript is actually over the size limit — archived rows
(`active = 0`) do not count, because they are not re-read. A session with 5,457 total rows but 242
active rows is not oversized.

### The guard never trips at all

Check that the config keys are reaching it. The guard reads `hermes_cli/config.py::load_config`
(not `gateway/run.py::_load_gateway_config`, which uses `load_user_config_effective()` and
deliberately does NOT merge `DEFAULT_CONFIG` — it would report every unset `runaway_*` key as
absent). A `runaway_*` key set to `0` disables that half of the rule.

## Dependencies

- `agent/compression_fleet_ceiling.py` — the ceiling constant and helpers (no external deps)
- `agent/context_compressor.py::_effective_threshold_cap` — where the ceiling binds
- `hermes_cli/config_defaults.py` — `DEFAULT_CONFIG["compression"]` (new keys deep-merge
  automatically, no config version bump needed)
- `gateway/session_lifecycle.py::_route_reset_reason` — the guard hook
- `gateway/runaway_session_guard.py` — the guard itself
- `hermes_state.py::_read_ctx` — the DB read path the guard uses (a `@contextmanager` yielding a
  connection)
- `hermes_cli/config.py::load_config` — the config reader the guard uses

## Measured effect

| Metric | Before | After |
|---|---|---|
| Fleet spend / day | $80.59 (2026-11-11), $96.02 (2026-11-12) | **$74.72** (2026-11-16) |
| Fleet avg cache-read / API call | 145,090 (11-11), 170,871 (11-12) | **87,648** (11-16) |
| Compaction events / 24h | 0 on sampled long sessions | **14** (gateway journal, 2026-11-16) |
| Trigger on a 1M window | ~500,000 (ratio only) | **120,000** |

**Validation of the guard's trip rule against the live fleet** (3,352 high-traffic sessions, all
live profile homes): **5 trip (0.15%)**, and all 5 are true positives — each idle > 130h with a
cache-read-per-call of 212k-361k (the healthy band is 17k-43k):

| profile | idle | active rows | live chars | cache-read/call | burned |
|---|---|---|---|---|---|
| librarian | 1,602h | 864 | 2,001,319 | 360,802 | 138M (~$8.29) |
| librarian | 1,546h | 1,089 | 2,253,684 | 361,058 | 174M (~$10.46) |
| shuri | 130h | 379 | 1,052,028 | 274,381 | 320M (~$19.18) |
| ultron | 454h | 341 | 818,940 | 290,718 | 44M (~$2.62) |
| root | 542h | 303 | 803,712 | 212,843 | 26M (~$1.55) |

Zero false positives: the healthy long-lived routes (root at 242 rows/1,678h, ultron main at
353 rows/1,190h) have small transcripts and do not trip.

**Why age alone is not a trip signal.** Across 11,262 measured live segments the age median is
1,730h and the max 3,898h — long-lived routes are *normal* on this fleet, because they are old
*and still in daily use*, and their transcripts stay small because they compact. Age and cost are
anti-correlated here: the older routes are old because they are still working.

## See Also

- `docs/wiki/services/hermes-compaction-loop-detector.md` — the usage-independent loop detector
  and bounded compaction cadence (shipped separately, commit `fde366b5df`)
- `Homelab/50-Agent-Memory/Shared/2026-11-16-compression-cap-all-homes.md` — the config cap proof
  across all 8 homes (Vision)
- `Homelab/50-Agent-Memory/Shared/2026-11-16-fleet-memory-hygiene-and-honcho-obsidian-routing.md`
  — the parallel memory-hygiene pass
- `Homelab/10-Services/Services/worker-memory-budget.md` — worker memory budget (a different budget:
  hot-injected files, not model context)
- Kanban cards `t_6abebfd8` (originating task) and `t_4770e5b3` (landing + doc correction)
