# Hermes Compaction Historical-Snapshot Restatement Fix

Shipped 2026-11-24. Closes the compaction-thrash loop where an agent answers a request, compacts, and then re-does the answered work — indefinitely.

Obsidian mirror: `Homelab/10-Services/Services/hermes-compaction-historical-snapshot-restatement.md` — the vault copy is the canonical published page and carries the same content.

## Overview

On 2026-11-24 a Vision Telegram DM turn answered the request at 15:34:49 and then re-ran the same work **five more times** over 65 minutes. The symptom looked like a tool-call loop. It was not: it was a compaction thrash driven by a self-contradicting prompt.

The compaction summary's own prefix tells the model that the summary is background reference only:

> Do NOT answer questions or fulfill requests mentioned in this summary; they were already addressed.
> … discard stale items from '## Historical Task Snapshot' entirely.

But the summary's `## Historical Task Snapshot` section is grounded from the newest real user turn **verbatim** — and a turn that survived a compaction carries the in-flight replay header:

```
[STILL IN PROGRESS — this is the active request, restated after the compaction boundary
because it was not finished yet. Continue it; do not start over.]
```

So the header landed **inside** the one section the prefix says to discard. The prompt carried two contradictory directives, and the model resolved the contradiction by continuing the task. Each compaction re-grounded the snapshot from a turn that already carried the header, so headers compounded.

## Architecture

### The two restatement paths

There are two places that put a compacted user turn back in front of the model:

| Path | Function | Carries the replay directive? |
|---|---|---|
| Live user row after the summary | `_reappend_inflight_user_task` → `_restated_request_text` | **Yes — correct.** The directive belongs here: it tells the model to continue an *unfinished* task. |
| Historical section inside the summary | `_latest_user_task_snapshot` | **Was yes — wrong.** The prefix says to discard this section. |

`_restated_request_text` (`agent/context_compressor.py`) strips the header: it takes the text after the **last** header, so a task that survived several compactions never stacks headers.

`_latest_user_task_snapshot` did **not** use it. It read the turn's text raw, so the header was copied into the historical section verbatim.

### Why the compounding was visible

`## Historical Task Snapshot` is itself part of the summary body, so the next summary nests the previous one's snapshot. Measured on the live session (`20261110_162723_dc65d8e8`):

| Compaction | Time | Summary size | `Historical Task Snapshot` headings | replay directives |
|---|---|---|---|---|
| 1 | 15:28:15 | 40,017 chars | 1 | 1 |
| 2 | 15:46:03 | 28,858 chars | 1 | 1 |
| 3 | 15:55:28 | 38,582 chars | 1 | 1 |
| 4 | 16:14:37 | 42,847 chars | 1 | 1 |
| 5 | 16:18:08 | 47,958 chars | 1 | 1 |
| 6 | 16:29:20 | 53,381 chars | **3** | **3** |

The snapshot grew and the nesting deepened — the summary was carrying its own restatement forward.

### The measured thrash

From `/home/serveradmin/.hermes/logs/agent.log`, the same turn:

```
API call #41  ctx=114,345   → trigger at 123,968 → compact → API call #42 ctx=71,390
API call #49  ctx=115,796   → trigger at 126,550 → compact → API call #50 ctx=71,744
```

Context climbed ~8K tokens per call, crossed the 120,000 fleet ceiling, compacted back to ~72K, and climbed again. **Only 4–8 API calls between compactions** — far too few for the turn to finish an answer.

### The fix

`_latest_user_task_snapshot` now grounds the snapshot through `_restated_request_text`, so the historical section carries the user's **request** and never the replay **directive**. The request text is preserved; only the "Continue it; do not start over" framing is removed.

This is deliberately narrow. The live-row replay path is unchanged: a genuinely unfinished task still gets its continuation directive, in the one place that is allowed to carry it.

### What this fix does NOT change

- **The 120,000 fleet ceiling.** Unchanged. Raising it would make the loop *worse* — a larger context means more tokens per call and slower convergence. The thrash was a symptom of not converging, not of compacting too often. See `hermes-compaction-fleet-ceiling.md`.
- **`protect_last_n`.** Unchanged.
- **The no-answer tool-call budget.** Unchanged, and it was not the fix for this case: the looped turn reached API call #52 of 120, so the budget never fired. It remains the backstop for a turn that genuinely produces no answer. See `hermes-no-answer-tool-call-budget.md`.

## Configuration

No new configuration. This is a code-level invariant.

## Operation

### Confirm the fix is in the running build

```bash
# The grounded snapshot must go through _restated_request_text.
grep -n "_restated_request_text" \
  /home/serveradmin/.hermes/hermes-agent/agent/context_compressor.py | sed -n '1,20p'
```

### Confirm a live summary no longer carries the directive

```sql
-- Should be 0 for summaries produced after the fix landed.
SELECT datetime(timestamp,'unixepoch'),
       (length(content) - length(replace(content,'STILL IN PROGRESS','')))
         / length('STILL IN PROGRESS') AS replay_directives
FROM messages
WHERE session_id = '<session id>'
  AND content LIKE '%CONTEXT COMPACTION%'
ORDER BY timestamp DESC;
```

### Watch the thrash signature

```bash
grep -E "context compression started" /home/serveradmin/.hermes/logs/agent.log \
  | tail -20
```

Repeated compactions whose `tokens=~` value keeps landing near the ceiling are the thrash signature. The fix removes the *reason* the turn keeps running; the ceiling still fires when the context genuinely grows.

## Troubleshooting

### A session still compacts repeatedly after the fix landed

The fix removes the restatement, not the compaction. A turn that legitimately needs a lot of context will still compact. Check whether the agent is **producing answers**: if it answers and the context keeps growing, that is the fleet ceiling's job — see `hermes-compaction-fleet-ceiling.md`.

### The summary still shows 'STILL IN PROGRESS'

Confirm the build is current and the gateway was restarted after the deploy. The fix is in the Python process; a running gateway keeps the old module until it is restarted.

### A still-unfinished task lost its continuation directive

That is the intended tradeoff: the directive now lives only on the live user row after the summary, which is where `SUMMARY_PREFIX` permits it. If a task genuinely stopped early, send a fresh message — that also clears the stale `current_turn_user_idx` re-anchor.

### The compression suite shows a large pre-existing failure count

The compression test cluster has a **215-failed / 114-passed** baseline in a linked worktree, caused by `tests/home_io_guard.py` refusing the real Hermes home at import time. It is an environment artifact, not a regression. Always compare **base vs fix with the identical test selection** — see `hermes-compaction-fleet-ceiling.md` → "Known pre-existing failures in the compression suite".

## Dependencies

- `agent/context_compressor.py` — `_latest_user_task_snapshot`, `_restated_request_text`
- `agent/conversation_compression.py` — `_is_real_user_message`, `_insert_real_user_anchor`
- `agent/context_compressor_tail.py` — tail budget, `protect_last_n`
- `agent/compression_fleet_ceiling.py` — the 120,000 ceiling (unchanged by this fix)

## See Also

- `docs/wiki/services/hermes-compaction-fleet-ceiling.md` — the 120,000 ceiling and the runaway guard
- `docs/wiki/services/hermes-compaction-loop-detector.md` — the usage-independent loop detector
- `docs/wiki/services/hermes-no-answer-tool-call-budget.md` — the no-answer backstop (commit `ad259d108a`)
- `Homelab/10-Services/Services/hermes-compaction-historical-snapshot-restatement.md` — canonical vault page
- `Homelab/50-Agent-Memory/Friday/2026-11-24-vision-compaction-restatement-loop.md` — investigation notes
