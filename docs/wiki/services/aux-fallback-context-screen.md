# Auxiliary Fallback Context Screen (t_e94832a1 / t_a08511cd)

> Wiki page for the fallback context-capacity screen.
>
> Status: **COMMITTED ON THE PORT BRANCH — NOT YET IN THE DEPLOYED TREE.**
>
> The screen was first authored in commit `57335d6bd20f2fabb1bff35fab7a46c5ec9f941e`
> ("fix(aux): screen fallback candidates by the request they must hold"), committed
> 2026-10-24 on `canonical-deploy`. That commit is **not an ancestor of the deployed
> HEAD** (`a7fa4d6789`), so the screen has never been live in the running tree.
>
> It was re-landed as a **port** onto live HEAD by `t_a08511cd`, because the live
> branch has moved ~17 300 commits past the fork branch the original was written
> on, and the two files it touches (`agent/auxiliary_client.py`,
> `agent/chat_completion_helpers.py`) have been rewritten substantially in the
> interim. The port is not a cherry-pick: it re-homes the fix's two mechanisms
> onto today's structure.
>
> Original incident: 2026-10-23 MiniMax plan-cap outage.
> Re-land task: `t_a08511cd` (repair card for the unresolved-merge tree).

## Overview

When a provider goes over quota, Hermes falls back to another provider. The
fallback chain screened candidates for entitlement, credential-pool exhaustion,
local usability, provider health and backend identity — but **never for context
capacity**. A fallback that switched to a model with a *smaller* window than the
session needed turned a provider outage into a hard 400.

During the 2026-10-23 MiniMax plan-cap incident the `local-llama` fallback took
the load. The llama.cpp server on ironman runs at `APP_CTX_SIZE=32768`, but the
conversation was ~182 472 tokens (session `20261007_031610_9596ddb5`, 244
messages). The server rejected every request:

```
E srv send_error: task id = 864, error: request (44622 tokens) exceeds the
available context size (32768 tokens)
```

### The leak

An auxiliary task with an **explicit** provider (`is_auto` is False, so the
configured chains are bypassed) whose provider ran out of credit landed on the
main-agent fallback rung — and that rung had **no context screen at all**. The
main agent had itself fallen back to the 32K local model, so compression
replayed the whole conversation onto it.

Three distinct mechanisms were missing, and the fix restores all three:

| # | Mechanism | Where | What it closes |
|---|---|---|---|
| 1 | **Request-size screen** | `agent/auxiliary_client.py` | A candidate that passes the task floor can still be too small for the request about to be replayed onto it. |
| 2 | **Real-endpoint resolution** | `agent/auxiliary_client.py` | The window lookup guessed from the model *name*, so a 32K llama-server was believed to have 128K. |
| 3 | **Agent-level twin** | `agent/chat_completion_helpers.py` | The main turn's fallback chain had no capacity screen at all — only the auxiliary path did. |

### Why the existing floor screen did not catch it

`agent/auxiliary_client.py` already had a floor screen (`_context_too_small`,
comparing a candidate's window against `MINIMUM_CONTEXT_LENGTH`). That answers
*"is this candidate big enough to be a sensible compressor at all?"* — not
*"can this candidate hold **the request we are about to send**?"*. A 64K model
passes the floor and then dies server-side.

And the window lookup fell through to a hardcoded catalog that matches on the
model **name**: `/app/models/qwen2.5-7b-instruct-q4_k_m.gguf` matched the `qwen`
catalog entry (131 072) even though the llama-server ran at 32 768. That is why
"context too small" never appeared in the 2026-10-23 logs — the screen believed
the local model had 128K and waved the 182K request straight through (#8731).

## Architecture

### The auxiliary path (`agent/auxiliary_client.py`)

```
auxiliary request fails
  └─ _aux_recovery_ladder  (shared by the sync and async callers)
       └─ _ladder_provider_fallback(required_tokens=_request_token_requirement(kwargs))
            ├─ _try_configured_fallback_chain   (auxiliary.<task>.fallback_chain)
            ├─ _try_main_fallback_chain         (main fallback_providers)
            └─ _try_main_agent_model_fallback   (the main agent's own model — THE LEAK)
```

`required_tokens` is measured once, from the outgoing wire kwargs, and threaded
into every rung. Each rung hands it to `_context_too_small`, which applies **two**
screens and rejects the candidate if either fails:

1. **the floor** (`min_ctx`) — `MINIMUM_CONTEXT_LENGTH`, and only for the
   `compression` task;
2. **the request size** (`required_tokens`) — the size of the prompt about to be
   replayed onto this candidate.

The larger of the two is the binding one, and the skip note says which:
`context too small: 32768<182472`.

### The agent-level twin (`agent/chat_completion_helpers.py`)

The main turn's fallback chain has its own gate,
`_should_skip_fallback_candidate`, which every chain entry passes through. The
twin adds two helpers:

- `_session_context_tokens(agent)` — the size of the session's current prompt.
  Prefers the provider's own last usage (anchored, so it accounts for messages
  appended since), then the compressor's last real prompt count. `None` means
  *unknown*.
- `_fallback_candidate_context_too_small(agent, fb, provider, model)` — the skip
  reason when the candidate's real window cannot hold that size.

### Unknown never blocks

Both screens **pass through** a candidate whose window is unknown (`None`), and
a session whose size is unknown. Refusing every unrecognised model would gut
the fallback surface; we can only refuse what we can *prove* is too small.
This is the single most important behavioural property of the fix.

## Operation

The screen is passive — it changes no configuration and has no operator toggle.
It applies to every auxiliary fallback chain walk and every agent-level fallback
chain entry.

### What a skip looks like in the logs

Auxiliary path (`agent.auxiliary_client`, INFO). Two lines are emitted when the
main-agent rung rejects a candidate — the candidate screen, then the fail-closed
notice:

```
Auxiliary compression: skipping main-agent(local-llama) (/app/models/qwen2.5-7b-instruct-q4_k_m.gguf context=32768 < min=64000), continuing chain
Auxiliary compression: skipping main-agent(local-llama) — its context window cannot hold this request (182472 tokens); failing closed instead of sending a request the server will reject
```

The first line names *which* requirement defeated the candidate: `min` is the
compression floor, `request` is the outgoing request size. The second names the
request size in tokens.

Agent-level path (`agent.chat_completion_helpers`, WARNING):

```
Fallback skip: local-llama//app/models/qwen2.5-7b-instruct-q4_k_m.gguf context window 32768 cannot hold this session's ~182472 tokens; skipping instead of sending a request the server would reject
```

All three name the candidate's real window and the requirement that defeated it.

### The 2026-10-23 incident in one line

```
Auxiliary compression: skipping main-agent(local-llama) (… context=32768 < request=182472), continuing chain
```

That line is the fix working. Before the fix, no such line was emitted at all —
the candidate's *name-based* window (131 072) looked large enough, so the 182K
request was forwarded and rejected.

## Configuration

No new configuration. The screen reads configuration it already had:

- `auxiliary.<task>.fallback_chain` — the per-task candidate list.
- `model.fallback_providers` — the main agent's fallback chain.
- The named custom provider's own `base_url` — resolved via
  `_custom_health_base_url(provider)`, which is what makes the *real* window
  visible instead of the catalog's name-based guess.

The window of a candidate comes from `agent.model_metadata.get_model_context_length`,
which honours `model.context_length` overrides. **Set `model.context_length`
on a local server whose window the catalog would guess wrong** — that override
is what the screen reads when the endpoint cannot be probed.

## Troubleshooting

**"context too small" appears for a candidate you expect to work.**
The screen read the candidate's real window, and the request really is bigger.
Either the session genuinely exceeds that model's window (fall back to a bigger
model), or the model's window is misreported. Check `model.context_length` for
that provider; the override is what the screen uses.

**"context too small" appears for a local llama.cpp server that you know is large.**
The window was resolved from the model's *name* because the provider's endpoint
was not resolvable. Check that the named custom provider is in the config with
its `base_url` — a named provider carries its endpoint in the **config**, not in
the fallback entry.

**A fallback that used to work is now being skipped.**
That is the fix behaving as intended: the candidate cannot hold the request.
Confirm by comparing the session size in the log with the candidate's window.

**The screen never fires.**
Expected when the session size is unknown (`_session_context_tokens` returns
`None`) or the candidate's window is unknown (`get_model_context_length` returns
`None` or raises). Both are deliberate pass-throughs, not failures.

**The deployed tree does not have the screen.**
Check the deployed tree, not a branch:
`grep -c '_fallback_candidate_context_too_small' agent/auxiliary_client.py`.
Zero means the fix is not deployed — see the status banner above. This is the
`deploy-tree-hygiene` failure shape: a fix committed on a branch is not a fix in
production.

## Dependencies

| Dependency | Used for |
|---|---|
| `agent.model_metadata.get_model_context_length` | resolving a candidate's real context window |
| `agent.model_metadata.estimate_messages_tokens_rough` | sizing the outgoing aux prompt |
| `agent.usage_anchor.anchored_context_tokens` | sizing the agent-level session prompt |
| `agent.auxiliary_health._custom_health_base_url` | resolving a named custom provider's real endpoint |
| `hermes_cli.fallback_config.get_fallback_chain` | the main fallback chain (existing) |

## See Also

- `docs/wiki/services/duplicate-tool-call-ids.md` — the sibling land-on-the-live-branch
  port, and the same `deploy-tree-hygiene` shape.
- `docs/wiki/services/lsp-gateway-cgroup-footprint.md`
- Original commit: `57335d6bd20f2fabb1bff35fab7a46c5ec9f941e` on `canonical-deploy`.
- Incident card: `t_e94832a1` (2026-10-23 MiniMax plan-cap outage).
- Re-land card: `t_a08511cd`.
- Tests: `tests/agent/test_auxiliary_fallback_context_bound.py`,
  `tests/agent/test_fallback_candidate_context_bound.py`.
