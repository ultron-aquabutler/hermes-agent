# Hermes Compaction Loop Detector & Bounded Compaction Cadence

The usage-independent loop detector and the minimum compaction interval are the
two guards that stop a Hermes session from compacting forever. They exist because
the original anti-thrash breaker could not fire on the providers we actually run
MiniMax on.

Written 2026-11-17 by @friday, alongside the fix (Hermes commit `fde366b5df`,
kanban `t_ca900eb9`). The canonical Obsidian page for the wider compaction
picture is `Homelab/20-Infrastructure/Hermes/Hermes-Context-Compaction-and-Runaway-Session-Guards.md`;
this page covers the loop detector and the cadence guard specifically.

## Overview

`should_compress()` gates on the provider's **real** `prompt_tokens`. The
anti-thrash breaker (`_ineffective_compression_count`) reaches its verdict in
`update_from_response()` — that is, it depends on the provider **reporting usage**.

**MiniMax omits usage.** So that verdict never arrives, the breaker never trips,
and the session compacts forever.

Observed on the Vision/default profile: session `20261110_162723_dc65d8e8`
reached **56 compactions in one session**, roughly one every **9–11 minutes**. The
message count churned 1,448 → 1,280 → 1,062 → 70 → 20 with no net progress.

Two independent guards now cover this, both of which work **without** provider
usage:

| Guard | Code | Scope | Effect |
|---|---|---|---|
| Usage-less loop detector | `_record_usage_less_loop_verdict()` | Providers that omit usage | Pauses automatic compaction for **300 s** |
| Minimum compaction interval | `_MIN_COMPACTION_INTERVAL_SECONDS = 120` | **Every** provider | Defers a repeat compaction for **120 s** |

## Architecture

### The discriminator: compaction effect, not session size

The first cut of the detector tripped on *"the rough estimate is still over the
threshold"*. Measured against the real runaway session (24 compaction
boundaries), that is wrong:

| Boundary | Rough estimate | Message savings | Verdict |
|---|---|---|---|
| 1 | 1,410,169 | 60.7 % | progress |
| 2 | 1,329,152 | 5.7 % | progress |
| **3** | 1,329,148 | **0.0 %** | **no progress** |
| **4** | 1,329,147 | **0.0 %** | **no progress → trip** |
| 8 | 1,169,496 | 11.6 % | progress (streak reset) |
| 16 | 574,913 | 47.7 % | progress |
| 18 | 124,142 | 78.2 % | progress |
| 19 | 116,131 | — | **under threshold** (streak cleared) |

The threshold is 120,000 tokens. Boundaries 1–8 sit **~11× over it** purely
because that session's schema-heavy tool set is enormous — yet they were still
reclaiming message tokens. Keying on size alone would have paused a **healthy**
session.

Keying on savings alone would miss the inverse case, where the incompressible
floor (system prompt + tool schemas) is what is over the line and the messages
are already tiny.

The detector therefore requires **both**:

1. the request is **still over the line** (`rough_tokens` vs `threshold_tokens`), and
2. this compaction **reclaimed nothing**
   (`_last_compression_savings_pct < _USAGE_LESS_LOOP_MIN_SAVINGS_PCT`, 2 %).

`_last_compression_savings_pct` is the messages-vs-messages shrinkage that
`compress()` already computes — the same basis the existing anti-thrash guard
uses, and the correct one, because the system prompt and tool schemas are an
incompressible floor that no amount of summarising can shrink.

### Constants

| Constant | Value | Meaning |
|---|---|---|
| `_USAGE_LESS_LOOP_TRIP_COUNT` | 2 | Consecutive no-progress boundaries before pausing |
| `_USAGE_LESS_LOOP_MIN_SAVINGS_PCT` | 2.0 | Minimum message-token reclaim that counts as progress |
| `_USAGE_LESS_LOOP_BLOCK_SECONDS` | 300.0 | How long automatic compaction stays paused |
| `_MIN_COMPACTION_INTERVAL_SECONDS` | 120.0 | Floor on the gap between two compactions (all providers) |

### Why the 2 % savings floor

Measured from the real runaway session:

- the **loop** phase reclaimed **0.0 %** against a ~1.33M estimate — a rounding
  error, i.e. compaction was spinning;
- the **marginal** phase reclaimed 0.5 %–1.3 % — still noise at that scale, and
  correctly classified as no-progress;
- the **healthy** phase reclaimed 5.7 %, 11.6 %, 47.7 % and 78.2 % — genuine
  work.

2 % sits above the noise floor and below every genuine reclaim observed.

### Both guards are bounded, never permanent

- the loop pause **expires** after 300 s, and is lifted **immediately** once a
  compaction demonstrates progress again;
- neither touches `_ineffective_compression_count`, so the permanent breaker
  (#97488 session-wipe territory) is never struck by a heuristic;
- manual `/compress` (`force=True`) never reaches the automatic gate at all;
- the provider-**proven** overflow path (`ignore_cooldown=True`) bypasses both
  guards — the provider has already rejected the request for exceeding its
  context, so compaction must be allowed through or the turn fails.

### Transient-block classification

The new reasons (`usage_less_loop`, `min_interval:<s>`) are classified as
*transient* in `conversation_compression.py::_mark_compression_blocked_transient()`.
A deferred pass is therefore **not** counted toward `compression_exhausted` —
otherwise a session merely cooling down would be wiped by the overflow
auto-reset.

### Not durable

The detector state lives in memory on the compressor instance; a gateway restart
clears it. That is acceptable: the pause is a short-lived brake, and the minimum
interval is recomputed from the last completed compaction.

## Operation

### Confirm the guards are in the running build

They are not configuration flags — they are always on. To confirm the code is
present:

```bash
cd /home/serveradmin/.hermes/hermes-agent
git log --oneline -1 -- agent/context_compressor.py     # expect fde366b5df
grep -n "_USAGE_LESS_LOOP_TRIP_COUNT\|_USAGE_LESS_LOOP_MIN_SAVINGS_PCT\|_USAGE_LESS_LOOP_BLOCK_SECONDS\|_MIN_COMPACTION_INTERVAL_SECONDS" \
  agent/context_compressor.py
```

### Watch the detector in the act

```bash
grep -E "Compaction did not shrink the request|Compaction loop detected without provider usage" \
  ~/.hermes/profiles/<profile>/logs/agent.log
```

- `Compaction did not shrink the request…` — the **first strike** (one boundary
  short of a trip).
- `Compaction loop detected without provider usage…` — the **trip**; automatic
  compaction is now paused for up to 300 s.

Both are suppressed under `quiet_mode`.

### Inspect the live signals for a session

```bash
/home/serveradmin/.hermes/hermes-agent/venv/bin/python \
  /home/serveradmin/.hermes/profiles/friday/cache/scratch/compaction_probe3.py
```

That prints, per compaction boundary of session `20261110_162723_dc65d8e8`, the
rough estimate and the messages-vs-messages savings — the two numbers the
detector combines.

### Confirm compaction is paused, and why

The block reason is surfaced through `_compression_block_reason()`:

| Reason | Meaning | Duration |
|---|---|---|
| `usage_less_loop` | The usage-less detector proved a loop | ≤ 300 s, lifted early on progress |
| `min_interval:<s>` | Too soon after the last completed compaction | ≤ 120 s |
| `cooldown:<s>` | Summary-model failure cooldown | profile-defined |
| `structural_backoff:<s>` | Structural no-op backoff | profile-defined |
| `ineffective` | Provider-**confirmed** ineffective compaction | **permanent** |

The first four are **transient**: they defer a pass without counting toward
`compression_exhausted`. Only `ineffective` is permanent, and only the
provider-confirmed path can produce it.

## Configuration

No new configuration keys. Both guards are unconditional, and their constants
are class attributes on `ContextCompressor`.

The only related knob remains `compression.threshold_tokens` (the line the
detector compares against) and `compression.enabled`.

## Troubleshooting

### A session compacts repeatedly but the context does not shrink

This is the usage-less loop, and it is now guarded — but the guard only helps if
you can tell it apart from healthy large-session compaction.

Check the two signals together:

1. **Is the request still over the line?** The rough estimate at the boundary
   (`compaction_probe3.py`) versus `threshold_tokens`.
2. **Is compaction reclaiming anything?** The messages-vs-messages savings.

If the estimate is over the line **and** savings are ~0 %, it was the loop, and
the detector should have paused automatic compaction for up to 300 s. If savings
are 1 %–60 %, the session was making real progress and pausing it would have
been wrong — a large tool set alone is not a loop.

If you see repeated compactions **with** healthy savings, the cause is elsewhere:
usually the trigger is too close to the post-compaction footprint, or the floor
(system prompt + tool schemas) is what is over the line and the messages are
already tiny.

### Compaction stopped entirely after a runaway

Expected if the loop detector tripped: automatic compaction is paused for up to
300 s, and lifts early as soon as a compaction shows progress. Confirm:

- the agent log shows `Compaction loop detected without provider usage`, and
- the session's estimate has since dropped below `threshold_tokens`.

**Manual `/compress` still works during the pause** — the manual path never
reaches the automatic gate. If you need to compact right now, use it.

If automatic compaction never resumes after the pause expires, check that the
running build actually carries commit `fde366b5df`; the detector state is
in-memory and is cleared by a gateway restart, but the *code* must be present
for the guard to exist at all.

### A session that was mid-runaway now looks stuck

The pause defers automatic compaction, but the session keeps making tool calls
and the transcript keeps growing. The right response is to let the pause expire
(or restart the gateway, which clears the in-memory detector state) and then
rotate the session. The detector is a brake on *compaction churn*, not a
substitute for session rotation.

## Dependencies

| Component | Role |
|---|---|
| `agent/context_compressor.py` | Owns both guards, the constants, and the block reasons. |
| `agent/conversation_compression.py` | Classifies the new reasons as transient blocks. |
| `agent/model_metadata.py` | `estimate_request_tokens_rough()` — the rough full-request estimate used as the over-the-line signal. |
| `tests/agent/test_compaction_usage_less_loop.py` | 20 tests; replays the real boundary table above. |
| `tests/agent/test_compaction_anti_thrash.py` | The pre-existing anti-thrash suite this guard sits alongside. |

## See Also

- `Homelab/20-Infrastructure/Hermes/Hermes-Context-Compaction-and-Runaway-Session-Guards.md`
  — the canonical Obsidian page for the wider compaction picture: trigger
  derivation, the fleet large-window ceiling, profile inheritance, and the
  routed-session runaway guard.
- `docs/wiki/services/gateway-state-db-integrity-bounds-and-turn-lease-reclaim.md`
  — the gateway-side session bounds that complement this agent-side brake.
- Hermes issue #97488 — the permanent-breaker session-wipe class of defect that
  the bounded-pause design deliberately avoids.
