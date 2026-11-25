---
title: Hermes Compaction — Original Tool Results in Summaries (Re-Grounding Loop Fix)
date: 2026-11-25
updated: 2026-11-25
agent: Friday
tags: [hermes, compression, gateway, token-budget, vision, loop-fix, prompt-injection]
status: landed
repo_doc: docs/wiki/services/hermes-compaction-summary-original-results.md
---

# Hermes Compaction — Original Tool Results in Summaries (Re-Grounding Loop Fix)

Shipped 2026-11-25. Closes the re-grounding loop where a compaction wrote its
summary from the *pruned* tool results, so the summary could not record what had
been read — and the next agent re-read the same artifact.

Obsidian mirror: `Homelab/10-Services/Services/hermes-compaction-summary-original-results.md`
— the vault copy is the canonical published page and carries the same content.

## Overview

A compaction wrote its summary from the **pruned** tool results instead of the
originals. A pruned result is a *stub* — a one-line placeholder naming the tool
and the byte count (`[read_file] read /notes/sweep.md from line 1 (40,056 chars)`).
The stub names the artifact but carries **none of its content**, so the summary
could not record *what* was in the file. The next agent, reading that summary,
had no evidence the file had already been examined and re-read it.

That is the re-grounding loop: a compaction that loses the evidence it exists to
preserve, producing the work it just finished.

Observed on the Vision Telegram session (`20261110_162723_dc65d8e8`, gateway key
`agent:main:telegram:dm:8943042074`):

- 135 summaries examined; **295** contained a `(N chars result)` stub and **23**
  were terminal stubs claiming `1 lines output`.
- `provider-quota-wall-card-sweep.md` was read **12 times**, each read landing
  right after a compaction.
- 16 early compactions in the window; 1,361 distinct short assistant preambles
  since 21:07 — the session was not converging.

This is a **distinct defect** from the
[historical-snapshot restatement](./hermes-compaction-historical-snapshot-restatement.md)
(fixed separately) and from the
[fleet ceiling / runaway guard](./hermes-compaction-fleet-ceiling.md).
The runaway guard did **not** fire during this session — the loop was not a
token-blowout; it was a loss of grounding.

## Architecture

### Two prune paths, and only one archives

This branch has two separate pruning implementations:

| Path | Module | Gated on | Writes the original? |
|---|---|---|---|
| **Compaction-window prune** | `ContextCompressor._prune_old_tool_results` (`agent/context_compressor.py:3546`) | unconditional (window planning) | **No** — stubs and discards |
| **Proactive reclaim** | `ProactivePruneMixin.prune_tool_results_only` (`agent/context_compressor_prune.py:105`) | cold cache, or `proactive_prune_tokens` | **Yes** — `archive_pruned_originals` writes to `cache/pruned/` and the stub names the file |

Only the proactive path archives. The compaction path has **nowhere** to put the
dropped original, so before this fix the original existed in exactly one place:
the transcript. Reading the stub instead of the original destroyed it.

### Why upstream's version is only *half* the fix

Upstream `NousResearch/hermes-agent` commit `d922b62d28` ("summarize the original
tool results first, archive after the summary") restores the originals for the
summary **window** only. Upstream can leave the head pruned because its
`_commit_window` / `_assemble_with_archive_list` machinery **archives** every
dropped original to `cache/pruned/` and appends an archive list to the committed
summary:

```
Archived tool output (exact originals; read_file a path to recover one):
```

This branch has **neither** the archive-list step nor `archive_compaction_drops`.
So a stubbed head row would be assembled into the final transcript and the
original lost for good. The port here therefore restores the original for
**every** pruned tool row, matching upstream's own stated invariant:

> results being summarized are never pointers, stubs or dedup references

### The three behaviours that were missing

Our branch forked `ec3249919a` (2026-11-10) and never took any of these:

1. **`4a45994971`** — the stub names the tool, the arguments and the line count,
   so a stub still carries identifying detail. (Cherry-picked as `1613885a4e`.)
2. **`d922b62d28`** — the summarizer reads the **original** tool results.
   (Ported, not cherry-picked — see below.)
3. **The `_is_summary_stub` gap** — `"(N chars result)"` was not recognised as an
   existing summary, so **existing stubs were summarized again**. Each
   re-summary compounded the loss.

### Why `d922b62d28` was ported rather than cherry-picked

Upstream's commit carries a different window architecture (`_commit_window`,
`_assemble_with_archive_list`) that conflicts materially with this branch's
prepared-compaction design. A blind cherry-pick conflicts in three files. The
*behaviour* was ported into the local architecture instead.

A full upstream merge was measured and rejected: **20 conflicted files**,
including the Telegram adapter, the kanban DB and the turn finalizer — all of
which carry local operational changes (the 120,000 fleet ceiling, the
no-answer tool-call budget, the runaway guard, the historical-snapshot fix).

### The fix in code

`agent/context_compressor_window.py` — new `summary_view(plan, originals, start, end)`:

```python
def summary_view(plan, originals, start, end):
    """*plan* with the ORIGINAL tool results put back: what the summarizer reads."""
    if len(originals) != len(plan):
        return plan
    calls = _tool_calls_by_id(originals)
    return [
        original if (original.get("role") == "tool"
                     and calls.get(original.get("tool_call_id") or "", ("",))[0]
                     not in _SUMMARY_KEEPS_PRUNED_FORM)
        else pruned
        for pruned, original in zip(plan, originals)
    ]
```

`agent/context_compressor.py` — in `compress()`, the summary view replaces
`messages` **wholesale**, before the stale-tail demotion and before the
handoff scan:

```python
summary_rows = summary_view(messages, canonical_messages, compress_start, compress_end)
messages = summary_rows
turns_to_summarize = messages[compress_start:compress_end]
```

Two ordering details are load-bearing:

- The view is taken **before** `_demote_stale_tail_tools`, otherwise the demotion
  would re-stub rows the view had just restored.
- `_scan_window_handoffs` is given the summary view so its rebuild does not
  reintroduce stubs and silently undo the fix.

## Configuration

No new configuration. The fix is in code.

Relevant existing settings (unchanged):

```yaml
compression:
  threshold: 0.5
  threshold_tokens: 300000
  target_ratio: 0.2
  protect_last_n: 20
```

Effective fleet ceiling: **120,000** tokens (see
[the fleet ceiling page](./hermes-compaction-fleet-ceiling.md)).

## Operation

### Confirm the fix is in the running build

```bash
cd /home/serveradmin/.hermes/hermes-agent
git log --oneline -3
# expect 2673197e76 fix(compression): summarize the ORIGINAL tool results, not the pruned stubs

grep -n "messages = summary_rows" agent/context_compressor.py
grep -n "for pruned, original in zip(plan, originals)" agent/context_compressor_window.py
```

### Run the invariant

```bash
/home/serveradmin/.hermes/profiles/friday/cache/scratch/run_iso_tests.sh \
  /tmp/summary_view.log tests/agent/test_compaction_summary_view.py
tail -3 /tmp/summary_view.log
# expect: 8 passed
```

### Run the surrounding compression suite

```bash
/home/serveradmin/.hermes/profiles/friday/cache/scratch/run_iso_pytest.sh \
  tests/agent/test_context_compressor.py
# expect: 299 passed
```

### Confirm a live summary carries content, not a stub

A healthy summary names what was read **and** what it said. An unhealthy one
carries only a `(N chars result)` stub.

```bash
python3 - <<'PY'
import re, sqlite3
db = sqlite3.connect("/home/serveradmin/.hermes/state.db")
rows = db.execute(
    "SELECT session_id, summary FROM messages "
    "WHERE role='summary' AND summary LIKE '%chars result%' ORDER BY id DESC LIMIT 20"
).fetchall()
print(f"{len(rows)} summaries still carry a stub")
PY
```

## Troubleshooting

### The session still re-reads the same artifact

Check the order of operations in `compress()`. If `_demote_stale_tail_tools` runs
**before** `summary_view`, the demotion re-stubs the restored rows and the fix
is inert.

### The stub is back but the summary looks fine

`_is_summary_stub` must recognise the `"(N chars result)"` form. If it does not,
existing stubs are summarized again on every subsequent compaction and the loss
compounds.

```bash
grep -n "_is_summary_stub" agent/context_compressor.py
```

### The test fails with `ImportError: cannot import name 'summary_view'`

The test module must not import `summary_view` at module level. It does not exist
on the base commit, and a module-level import fails *collection* — so the
behavioural test could never prove red. The new-API tests call the lazy
`_summary_view()` helper; the behavioural test uses only base-existing APIs
(`_fallback_anchors`) so it can actually prove red.

### The repo's own compression suite "fails" with 60+ guard errors

`tests/home_io_guard.py` refuses file I/O against the real Hermes home, and the
repo's `agent.conversation_loop` import chain reads `~/.hermes/manifest.json`
before the conftest sandbox applies. This is **pre-existing** — the same suite
fails identically on the base commit. Use the isolated-home runner.

### `scripts/run_tests.sh` hangs or is killed at 300s

The canonical runner's parallel bootstrap re-resolves the environment. The
direct pytest invocation against an isolated `HOME`/`HERMES_HOME` completes the
same suite in ~25s.

## Dependencies

- `NousResearch/hermes-agent` upstream commits `4a45994971` (cherry-picked) and
  `d922b62d28` (ported).
- `agent/context_compressor_prune.py` — upstream `terminal_stub`, `delegate_stub`
  and `stub_arguments` helpers from `4a45994971`.
- `agent/context_compressor_summary.py` — copied from upstream at `1613885a4e`.
- `tests/home_io_guard.py` — the guard that forces an isolated home for test runs.

## See Also

- [Hermes Compaction Historical-Snapshot Restatement](./hermes-compaction-historical-snapshot-restatement.md)
- [Hermes Compaction Fleet Ceiling & Runaway Session Guard](./hermes-compaction-fleet-ceiling.md)
- [Hermes No-Answer Tool-Call Budget](./hermes-no-answer-tool-call-budget.md)
