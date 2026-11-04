# Duplicate Provider Tool-Call Items (t_d0a68830)

> Wiki page for the duplicated-tool-call fix.
>
> Status: **NOT DEPLOYED**. The change lives on local branch
> `fix/duplicate-tool-call-ids-t_d0a68830` (worktree
> `/home/serveradmin/.hermes/hermes-agent/.worktrees/t_d0a68830-dupids`).
> Nothing was pushed, merged, deployed, or restarted.
>
> Incident: session `ultron/20261104_161428_2a9017` (2026-11-04 16:14 UTC).

## Overview

In one assistant turn the provider emitted **every tool call twice**: once with
the real arguments and once with `{}` — under the **same** `id`, the **same**
`call_id` and the **same** `response_item_id` (the provider's per-item
identity). Both copies reached dispatch, so each call ran twice: once usefully
and once as a malformed empty-argument call. The repeated empty calls are what
tripped the no-progress guardrail; the turn ended `reason=guardrail_halt`.

### The evidence (recorded, not inferred)

The assistant turns of the incident session are recorded in
`~/.hermes/profiles/ultron/state.db` (`messages.tool_calls`). Measured over
them:

| Quantity | Value |
|---|---|
| Assistant turns with tool calls | 7 |
| Tool-call entries across those turns | 45 |
| Distinct provider item ids | 28 |
| Item ids emitted more than once | 17 |
| Of those, copies with `{}` arguments | 17 (one empty copy each) |
| Item ids with two DIFFERENT non-empty argument sets | **0** |
| Tool results actually recorded | 45 |

Every duplicated item id had **exactly one** non-empty-argument copy and
**exactly one** empty-argument copy. A representative pair:

```json
{"id": "call_exjp35UJgCrAnh6XyXW7ZKat", "call_id": "call_exjp35UJgCrAnh6XyXW7ZKat",
 "response_item_id": "fc_exjp35UJgCrAnh6XyXW7ZKat", "type": "function",
 "function": {"name": "skill_view", "arguments": "{\"name\":\"hermes-profile-state-ownership\"}"}}
{"id": "call_exjp35UJgCrAnh6XyXW7ZKat", "call_id": "call_exjp35UJgCrAnh6XyXW7ZKat",
 "response_item_id": "fc_exjp35UJgCrAnh6XyXW7ZKat", "type": "function",
 "function": {"name": "skill_view", "arguments": "{}"}}
```

Both copies ran — the recorded tool results for that one call id are a
successful `skill_view` **and** a failed `skill_view` with
`"error": "Skill '' not found."` (the empty copy). The session's log line
`WARNING run_agent: Removed duplicate tool call: search_files` refers to the
**argument-level** dedup, which saw `{"path":...}` and `{}` as different calls
and removed neither.

> **What is NOT claimed.** The provider's raw SSE frames for this session were
> not captured, so the mechanism that produced the duplicate representation (a
> re-announced item, a bridge that echoes the call, a replay of the request) is
> **not established**. This page documents what the recorded payloads prove and
> the reconciliation built on that evidence — not a root cause for the
> provider's behaviour.

## Architecture

### The evidence key: the provider's item identity

`agent/transports/codex.py::normalize_response` builds every tool call as a
`ToolCall` carrying the provider's response-item id:

```
ToolCall(id=..., name=..., arguments=..., provider_data={"call_id":..., "response_item_id": "fc_..."})
```

`response_item_id` is the provider's **per-item identity** — it is derived from
the call id (`agent/codex_responses_adapter.py::_derive_responses_function_call_id`),
so two representations of the same call carry the same one.

That gives the rule the fix is built on:

| Evidence | Meaning | Action |
|---|---|---|
| Same `response_item_id` | ONE provider item, represented more than once | Collapse to one call; keep the copy with the **complete** arguments |
| Different `response_item_id` | TWO distinct provider items — two operations | Keep both; give each its own id |

The distinction matters: the argument-level rule cannot tell these apart. Two
calls to the same tool with byte-identical arguments under **different** item
ids are two legitimate operations (the model asked for the same thing twice);
collapsing them would silently discard an operation the caller asked for.

### Where each policy lives

| Layer | Module | Role |
|---|---|---|
| Stream assembler | `agent/codex_runtime.py::_CodexResponseAssembler._on_item_added` | Keeps the richest arguments when one item is announced twice |
| Policy owner | `agent/message_sanitization.py::provider_item_identity` | The evidence key |
| Policy owner | `agent/message_sanitization.py::reconcile_duplicate_tool_call_items` | Collapses one item represented twice |
| Policy owner | `agent/message_sanitization.py::uniquify_tool_call_ids` | Gives the surviving distinct items their own ids |
| Policy owner | `agent/message_sanitization.py::deduplicate_tool_calls` | Argument-level dedup, now identity-aware |
| Call site | `agent/turn_tool_validation.py::validate_tool_calls` | Runs the reconciliation before everything else |
| Call site | `run_agent.py::AIAgent._deduplicate_tool_calls` | Delegates to the owner |

### The order matters

```
reconcile_duplicate_tool_call_items   # ONE item seen twice -> ONE operation
        ↓
uniquify_tool_call_ids                 # distinct items that reused an id -> own ids
        ↓
deduplicate_tool_calls                 # same tool + same args + SAME item -> drop one
```

Reconciliation runs **first** because it is the only rule that can tell "one
operation seen twice" from "two operations". By the time the argument-level
rule runs, every surviving entry is a distinct provider item, so two entries
with identical arguments are two operations and both are kept.

### The stream assembler also had the defect

`agent/codex_runtime.py` keys announced calls by item id, and a repeated item id
**overwrote** the pending entry:

```python
self.pending_function_calls[item_id] = {..., "arguments": str(...), ...}
```

So a stream that announced the item with real arguments and then again with
`{}` ended up with the **empty** arguments — the call would have run with no
arguments at all. The assembler now keeps the richer arguments for a repeated
item id.

### The id repair is atomic and total

`uniquify_tool_call_ids` rewrites **every** spelling of the id together
(`id`, `call_id`, and the `provider_data` backing them), and the write is
**all-or-nothing**: every spelling is snapshotted first, and if any write
raises the snapshot is restored. A failed rename leaves the entry **untouched**
rather than half-renamed.

`_tc_set` routes property-backed fields to `provider_data`, because
`agent/transports/types.py::ToolCall` exposes `call_id` / `response_item_id` as
read-only properties over it. A plain `setattr` raises `AttributeError`
("property 'call_id' of 'ToolCall' object has no setter"), and the blanket
`except Exception` around the old rewrite swallowed it — leaving the entry with
a rewritten `id` and the original `call_id`, i.e. a **split-brain** whose
effective (coalesced) id was still the duplicated one.

### Determinism is preserved

Renamed ids are still `<id>_d<n>` with `n` starting at 2, chosen by a bounded
scan of already-taken suffixes. **No uuid4, no randomness** — these ids feed
prompt-cache prefixes, so a non-deterministic rename would invalidate every
cached prefix in the conversation.

## Operation

### Verify the reconciliation on the live tree

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python - <<'PY'
import sys; sys.path.insert(0, ".")
from agent.message_sanitization import reconcile_duplicate_tool_call_items
from agent.transports.types import ToolCall

calls = [
    ToolCall(id="call_A", name="skill_view", arguments='{"name":"x"}',
             provider_data={"call_id": "call_A", "response_item_id": "fc_A"}),
    ToolCall(id="call_A", name="skill_view", arguments="{}",
             provider_data={"call_id": "call_A", "response_item_id": "fc_A"}),
]
kept = reconcile_duplicate_tool_call_items(calls)
print("kept:", [(tc.name, tc.arguments) for tc in kept])
assert len(kept) == 1 and kept[0].arguments == '{"name":"x"}'
print("OK")
PY
```

Expected: `kept: [('skill_view', '{"name":"x"}')]` then `OK`.

### Replay the recorded incident turns

The recorded turns are the ground truth for this fix:

```bash
sqlite3 ~/.hermes/profiles/ultron/state.db "
SELECT id, tool_calls FROM messages
WHERE session_id='20261104_161428_2a9017' AND tool_calls IS NOT NULL AND tool_calls!=''
ORDER BY id;"
```

45 entries across 7 turns; 17 item ids appear twice, each as one full-arguments
copy plus one `{}` copy.

### Run the regression tests

```bash
cd /home/serveradmin/.hermes/hermes-agent
HERMES_PYTHON=/home/serveradmin/.hermes/hermes-agent/venv/bin/python \
  scripts/run_tests.sh tests/agent/test_duplicate_tool_call_ids.py -q
```

Expected: `28 tests passed, 0 failed`.

**Run from a plain directory copy, not a git worktree.** The test-only home-I/O
guard (`tests/home_io_guard.py`) refuses any path under the real `~/.hermes`.
A linked git worktree's `.git` is a FILE whose target is
`~/.hermes/hermes-agent/.git`, so Hermes' dependency activation trips the guard
from **any** worktree location — including `/tmp` — for every test that
imports `run_agent`. This is a pre-existing harness constraint, not a product
defect: the same suites fail identically on the untouched base tree.

### Check the live log for the symptom

```bash
grep -E 'Could not uniquify duplicate tool call id|Removed duplicate tool call|Collapsed duplicate representation' \
  ~/.hermes/profiles/*/logs/errors.log | tail -20
```

After the fix is deployed:

- `Collapsed duplicate representation of one provider tool-call item` — the
  reconciliation fired (one item seen twice, now runs once). **Expected.**
- `Could not uniquify duplicate tool call id` — the id repair failed. Must not
  appear; the repair is now atomic.
- `Removed duplicate tool call: <tool>` — the argument-level dedup removed a
  duplicate. Only a defect when it coincides with a failed uniquify in the same
  turn.

## Configuration

No configuration keys. The policy is unconditional.

| Knob | Effect |
|---|---|
| `_PROVIDER_TOOL_ID_PREFIXES = ("chatcmpl-tool-",)` | Unrelated to this fix: the provider-minted-id normalizer (`normalize_provider_tool_call_ids`) for replay compatibility. |

## Troubleshooting

### Symptom: a call ran twice (once usefully, once empty)

The provider emitted one item twice. Check whether the two entries share a
`response_item_id`:

```bash
sqlite3 ~/.hermes/profiles/<profile>/state.db "
WITH t AS (SELECT tool_calls FROM messages WHERE session_id='<sid>' AND tool_calls IS NOT NULL AND tool_calls!=''),
e AS (SELECT j.value AS v FROM t, json_each(t.tool_calls) j)
SELECT json_extract(v,'\$.response_item_id') AS rid,
       json_extract(v,'\$.function.name') AS nm,
       json_extract(v,'\$.function.arguments') AS args
FROM e ORDER BY rid;"
```

Same `rid` on two rows = one item seen twice = the reconciliation should have
collapsed them. Different `rid` = two genuine operations.

### Symptom: `Could not uniquify duplicate tool call id <id>` in the log

The id repair failed. With the atomic rewrite this means the tool-call object
exposes a `call_id` that cannot be written **and** has no writable
`provider_data` — a shape outside `ToolCall`. Check that the entry is one of:
a `dict` (writable by key), a `SimpleNamespace` / SDK model (writable by
attribute), or an `agent.transports.types.ToolCall` (writable through
`provider_data`).

### Symptom: a call the model asked for did not run

The pre-API chokepoint (`agent_runtime_helpers._dedupe_tool_call_ids`) dropped
it, or the argument-level dedup collapsed it. Confirm the drop was legitimate:

```bash
grep -n 'Pre-call sanitizer: removed .* duplicate tool_call_id' \
  ~/.hermes/profiles/*/logs/*.log | tail
```

### Symptom: a call ran with NO arguments although the model supplied some

The stream assembler saw the item announced twice and the empty copy won. The
assembler now keeps the richer arguments; a log line
`Collapsed duplicate representation of one provider tool-call item` shows the
reconciliation happened.

## Dependencies

- `agent/message_sanitization.py` — the provider-item-identity and call-id
  policy owners.
- `agent/codex_runtime.py` — the stream assembler that keeps the richest
  arguments for a repeated item id.
- `agent/turn_tool_validation.py` — runs the reconciliation before the id
  repair.
- `agent/transports/types.py` — `ToolCall` and its read-only `call_id` /
  `response_item_id` property views over `provider_data`.
- `agent/transports/codex.py` — builds the `ToolCall` shape.
- `agent/agent_runtime_helpers.py` — the pre-API chokepoint.
- `run_agent.py` — `AIAgent._deduplicate_tool_calls` delegates to the owner.

## See Also

- `tests/agent/test_duplicate_tool_call_ids.py` — the regression suite (28
  cases: one-item-twice collapses, distinct items all run, both argument
  orders, zero-argument calls, response-item aliases, suffix collisions,
  terminal outputs, writable and failing setters, rollback, and the
  stream→validation→dispatch path with a fake side-effect counter).
- `tests/agent/test_message_sanitization_policy.py` — the pre-existing policy
  tests for the same owners (unchanged and still green).
- `tests/agent/test_agent_guardrails.py` — the pre-existing argument-level dedup
  tests (unchanged and still green).
- `tests/agent/test_codex_responses_settle_pending_tool_calls.py` — the
  pre-existing stream-settlement tests (unchanged and still green).
- `docs/wiki/services/worker-memory-budget.md` — the other change on this branch
  (`canonical-deploy-t_7161d9a9`).
- t_d0a68830 — this card.
