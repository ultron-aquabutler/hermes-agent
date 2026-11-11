# LSP Servers in the Gateway Cgroup (t_2dfa12f1)

> Wiki page for the language-server accumulation fix in the gateway's own cgroup.
>
> Status: **COMMITTED ON THE LIVE BRANCH `fix/gateway-outage-patches-t_1915c9a3`.
> The heap cap is ALREADY LIVE; the key-sharing fix and the cgroup exclusion
> need a gateway restart.**
>
> The running gateway (pid 2244084, started 2026-11-11 13:16:31 UTC) imported
> `agent/lsp/manager.py` and `agent/lsp/servers.py` **after** the heap-cap
> change, so the live language servers already carry the cap — verified in
> `/proc/<pid>/environ`:
>
> ```
> NODE_OPTIONS=--max-old-space-size=512
> ```
>
> The **key-sharing** fix (`UNTRUSTED_SHARED_ROOT`) and the **cgroup-exclusion**
> work (`gateway/session_worker_scope.py`) were written after that import, so
> they are NOT active until the gateway restarts. A restart is a separate,
> human-gated step (see `t_33a986c1`): it tears down the unit cgroup and kills
> in-flight workers, and it requires Bryan's explicit permission.
>
> Regression: `tests/agent/lsp/test_resource_bounds.py` -> 29 passed.
> Cgroup isolation: `tests/gateway/test_managed_worker_cgroup_isolation.py`
> -> 14 passed.
> Broader suite: `tests/agent/lsp/` -> 135 passed, 10 failed — the 10 failures
> are **pre-existing** and reproduce identically on the untouched base tree
> (the test-only home-I-O guard refusing file I/O against the real `~/.hermes`,
> and package-manager layout assumptions). See Operation.

## Overview

The gateway was killing **itself**. Not via the systemd watchdog — via its own
event-loop liveness watchdog, after memory pressure starved the loop.

```
unbounded LSP servers in the gateway cgroup (~2 GB, growing)
  -> working set exceeds memory.high (pinned at 1800/1800)
  -> continuous reclaim (~5,800 throttle events/min), 3.1 GB swapped
  -> the event loop is starved during reclaim
  -> an auxiliary-LLM config deepcopy (GIL-bound) makes it fatal
  -> gateway/shutdown_watchdog.py fires: "event loop missed 3 consecutive
     liveness probes"
  -> gateway exits 75 and the supervisor restarts it
```

Every in-flight Kanban card died with it, because the gateway owns the
admission ledger. Two kills on 2026-11-11 (`04:15:48`, `08:58:26`) took **12
cards across 2 boards** down as collateral.

### The language servers live in the GATEWAY's cgroup

`agent/lsp/manager.py` spawns them with `subprocess`/`asyncio` from inside the
gateway process, so they are children of the gateway — **not** of a worker
scope. A worker scope has its own `MemoryMax`; the gateway cgroup does not
protect its children that way.

Live measurement (2026-11-11 04:00–04:40, gateway cgroup):

| PID | Profile | RSS | Swap |
|---|---|---|---|
| 391368 | friday | 510 MiB | — |
| 406924 | friday | 492 MiB | — |
| 382772 | root | 376 MiB | 111 MiB |
| 389920 | librarian | 140 MiB | 454 MiB |

**Aggregate: 5 LSP processes = 865 MiB RSS + 1,093 MiB swap ≈ 2 GB** of a
1800 MiB `memory.high`.

### The growth mechanism (the root cause)

A second friday LSP spawned **while the first was still resident**:

```
pid=391368  friday  age=04:02  rss=510 MiB
pid=406924  friday  age=00:53  rss=492 MiB   <- NEW, already 492 MiB
```

That is not a leak of *orphans* — it is the **client key**. `agent/lsp/manager.py::_client_key`
picks the cache key for a language-server client:

```python
return (srv.server_id, "" if srv.multi_root and trusted else root)
```

Pyright is a `multi_root` server (`agent/lsp/servers.py`, `multi_root=True`), so
*trusted* roots all share one process under the key `("pyright", "")`. But the
shared key requires `trusted=True`, and **an untrusted root gets its own key**
— `("pyright", <root>)` — and therefore **its own process**.

And a Kanban worker has **no operator anchor**:

```python
# agent/lsp/workspace.py::operator_workspace_roots()
if os.environ.get("HERMES_KANBAN_TASK"):
    return set()
```

Work the model can schedule has no operator anchor: a Kanban worker is launched
in the task's workspace, and the `kanban_create` / `cronjob` tools let the model
pick both. So **every root a worker touches is untrusted**, every untrusted root
gets a per-root key, and every distinct checkout a worker turns on adds another
live pyright process to the gateway's cgroup.

Verified empirically on the live tree:

```
pyright multi_root: True

  /home/serveradmin/.hermes/hermes-agent  trusted->('pyright', '')  untrusted->('pyright', '/home/serveradmin/.hermes/hermes-agent')
  /home/serveradmin/.hermes/profiles/friday trusted->('pyright', '')  untrusted->('pyright', '/home/serveradmin/.hermes/profiles/friday')
  /tmp/some-other-repo                   trusted->('pyright', '')  untrusted->('pyright', '/tmp/some-other-repo')

  with HERMES_KANBAN_TASK set: set()        <- a worker: NO anchor, EVERY root untrusted
  without:                    {'/home/serveradmin/.hermes/hermes-agent'}
```

This is the same mechanism the sibling page
[[Homelab/10-Services/Services/worker-memory-budget|Worker Memory Budget]]
identified at the **host** level. This page covers the **gateway cgroup**
accumulation and the crash it causes.

### Why the idle reaper did not help

`agent/lsp/manager.py` has `_start_idle_reaper` / `_reap_idle_once`, and the
manager keeps `self._clients: dict[_Key, LSPClient]`. But these servers were
**not idle** — they were actively serving turns — so the idle reaper never
reaped them. The accumulation was of *active* clients, one per root, not of
idle ones.

## Architecture

### Three client keys, one process per key class

The fix changes the key shape so that untrusted roots of a *trust-safe*
multi-root server **share one process**:

| Key | Who shares it | Why it is safe |
|---|---|---|
| `("pyright", "")` | every **trusted** root | The trusted client is pinned to the first root's **project** venv (`.venv`/`venv`) — it may load that project's code. |
| `("pyright", UNTRUSTED_SHARED_ROOT)` | every **untrusted** root | The untrusted client is pinned to the **operator's own** interpreter (`VIRTUAL_ENV`) — it never loads a checkout's code, whichever root is attached to it. |
| `("pyright", <root>)` | one root only | Every other case: a non-multi-root server, or a multi-root server that is **not** safe to run without trust. |

`UNTRUSTED_SHARED_ROOT` is the sentinel `"\x00untrusted-shared"`. It is
deliberately **not** the empty string (that is the trusted key) and it is
deliberately **not** a plausible filesystem path, so it can never collide with
a resolved project root.

### The trust gate still decides what the server may DO

The sharing does **not** relax workspace trust. The gate is applied at spawn
time, in `agent/lsp/servers.py::_spawn_pyright`:

```python
# Pyright executes this interpreter, so a checkout's own venv is only used when the workspace is trusted.
py = _detect_python(root if ctx.trusted else None)
```

An untrusted spawn resolves the interpreter from `VIRTUAL_ENV` (the operator's
environment) or the Hermes-installed Python — never from `<root>/.venv`. So an
untrusted client is safe to share across untrusted roots: it cannot load
project code no matter which root is attached.

The sharing applies **only** to servers listed in
`agent/lsp/servers.py::UNTRUSTED_SAFE_SERVERS` — the servers whose own
documentation says they do not run project code:

```python
UNTRUSTED_SAFE_SERVERS = frozenset({
    "pyright", "typescript", "svelte-language-server", "bash-language-server",
    "yaml-language-server", "dockerfile-ls", "intelephense", "clangd",
})
```

A multi-root server **outside** that list keeps its per-root key. Sharing one
would let it serve roots it is not permitted to serve.

### The trust class is derived from the KEY, not from the root

`LSPService._get_or_spawn` computes the spawn's trust flag from the resolved
key:

```python
trusted = key[1] != UNTRUSTED_SHARED_ROOT and (key[1] == "" or self._trusted(root))
```

Trust only grows over a process's lifetime, so a single read taken *before*
the lock could otherwise store an untrusted spawn under the shared multi-root
key that every trusted root then attaches to. Deriving trust from the key
removes that window by construction.

`LSPService._live_key` was extended to find an already-live shared untrusted
client (instead of computing a fresh per-root key), so a second untrusted root
**attaches** to the running process via
`workspace/didChangeWorkspaceFolders` rather than spawning a second one.

### The two resource bounds (from t_67049b90, same change)

These bound the *per-process* footprint and the *process count*; the key fix
above removes the *reason* the count grew.

| Key | Default | Effect |
|---|---|---|
| `lsp.max_server_heap_mb` | `512` | V8 heap ceiling for node-based servers, applied as `NODE_OPTIONS=--max-old-space-size=512`. A user-set `--max-old-space-size` **wins** — the operator's explicit choice is not overridden. `0` = no cap. |
| `lsp.max_clients` | `2` | Maximum number of **live** language-server processes per Hermes process. Over the cap, the **least-recently-used** client is shut down; its next use respawns it. `0` = unlimited. |

The heap cap makes a pathological server **fail** (V8 aborts) instead of
eating the gateway. A language server that dies is restarted on demand; a
gateway that dies takes the fleet's control plane with it.

### The measured before/after (2026-11-11, this card)

The key fix was measured against the OLD key shape
(`(server_id, "" if multi_root and trusted else root)`) on the live tree:

```
=== UNTRUSTED root (kanban worker: operator_workspace_roots() == set()) ===
  /home/serveradmin/.hermes/hermes-agent
     old=('pyright', '/home/serveradmin/.hermes/hermes-agent')
     new=('pyright', '\x00untrusted-shared')
  /home/serveradmin/hermes-test-copy
     old=('pyright', '/home/serveradmin/hermes-test-copy')
     new=('pyright', '\x00untrusted-shared')
  /home/serveradmin/hermes-workspace
     old=('pyright', '/home/serveradmin/hermes-workspace')
     new=('pyright', '\x00untrusted-shared')
  /tmp/other-checkout
     old=('pyright', '/tmp/other-checkout')
     new=('pyright', '\x00untrusted-shared')

old distinct keys for 4 untrusted roots: 4 -> 4 PROCESSES
new distinct keys for 4 untrusted roots: 1 -> 1 PROCESS
```

The two trust classes together, which is the real shape of a busy gateway
(one anchored profile turn + kanban worker turns on other checkouts):

```
                       old procs   new procs
anchored turn             1            1
worker turn (4 roots)     4            1
TOTAL                     5            2
```

**Why a second pyright existed for one profile** (the card's original
evidence, "age 04:02 AND age 00:53 simultaneously"): the two processes were
two different **trust classes** — the anchored turn's trusted client (key
`("pyright", "")`) and a worker turn's untrusted client (key
`("pyright", <root>)`). The anchored/untrusted split is deliberate and stays:
the trusted client is pinned to a project venv and may load project code, the
untrusted one never does. What was wrong was that the untrusted side spawned
**one process per checkout** instead of one per server.

### The heap cap is verifiable in the LIVE process

```bash
for p in $(pgrep -f 'pyright-langserver'); do
  echo "$p: $(tr '\0' '\n' </proc/$p/environ | grep NODE_OPTIONS)"
done
```

Expected on a fixed tree: `NODE_OPTIONS=--max-old-space-size=512`.

## Operation

### Verify the key shape on the live tree

```bash
cd /home/serveradmin/.hermes/hermes-agent
cat > /tmp/verify_lsp_key.py <<'PY'
import sys; sys.path.insert(0, "/home/serveradmin/.hermes/hermes-agent")
from agent.lsp.manager import _client_key, UNTRUSTED_SHARED_ROOT
from agent.lsp.servers import SERVERS
srv = next(s for s in SERVERS if s.server_id == "pyright")
a = _client_key(srv, "/repo-a", trusted=False)
b = _client_key(srv, "/repo-b", trusted=False)
print("untrusted a:", a)
print("untrusted b:", b)
print("shared      :", a == b)
print("vs trusted  :", _client_key(srv, "/repo-a", trusted=True))
assert a == b, "untrusted roots must share ONE client"
assert a != _client_key(srv, "/repo-a", trusted=True), "trusted must stay separate"
print("OK")
PY
./venv/bin/python /tmp/verify_lsp_key.py
```

Expected: `shared: True`, `vs trusted: ('pyright', '')`, then `OK`.

### Count the live language-server processes in the gateway cgroup

```bash
C=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service
for p in $(cat "$C/cgroup.procs"); do
  if grep -qa 'langserver' "/proc/$p/cmdline" 2>/dev/null; then
    echo "pid=$p rss=$(awk '/VmRSS/{print $2}' /proc/$p/status)K $(tr '\0' ' ' </proc/$p/cmdline | cut -c1-90)"
  fi
done
```

**Before the fix** this could show several pyright processes for the same
profile at once (one per root). **After the fix and a gateway restart** it
shows at most one pyright per profile.

### Watch the gateway's memory headroom and throttle rate

```bash
C=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service
cat "$C/memory.current" "$C/memory.high" "$C/memory.swap.current"
grep '^high' "$C/memory.events"
```

`memory.events: high` is a **cumulative counter** of throttle events. Sample
it twice to get a rate:

```bash
C=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service
a=$(grep '^high' "$C/memory.events" | awk '{print $2}'); sleep 60
b=$(grep '^high' "$C/memory.events" | awk '{print $2}')
echo "throttle events/min: $(( (b-a)/60 ))"
```

The card's baseline was **~5,800/min** — a continuously thrashing gateway.
After this lands the rate should return to ~0 under normal load.

### Run the regression tests

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python -m pytest tests/agent/lsp/test_resource_bounds.py -q
```

Expected: `29 passed`.

### The broader LSP suite, and how to read its failures

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python -m pytest tests/agent/lsp/ -q -p no:cacheprovider
```

Expected on the live tree: `135 passed, 10 failed, 1 skipped`.

**The 10 failures are pre-existing and are NOT caused by this change.** They
reproduce identically with the change stashed:

```bash
cd /home/serveradmin/.hermes/hermes-agent
git stash push agent/lsp/manager.py -m "baseline-check"
./venv/bin/python -m pytest tests/agent/lsp/ -q -p no:cacheprovider --ignore=tests/agent/lsp/test_resource_bounds.py
git stash pop
```

Two failure classes:

1. **`TEST BUG: file I/O against the REAL hermes home`** — the test-only
   home-I/O guard (`tests/home_io_guard.py`) refuses any path under the real
   `~/.hermes`. This is a harness constraint, not a product defect.
2. **Package-manager layout assumptions** — tests that expect an install
   layout the host does not have.

Both fail the same way on the untouched base tree. Do not read them as
regressions from this page's change; confirm with the stash-and-rerun above.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `lsp.max_server_heap_mb` | `512` | V8 heap ceiling (MiB) for node-based language servers. `0` disables. A user-set `--max-old-space-size` in `lsp.servers.<id>.env` wins. |
| `lsp.max_clients` | `2` | Maximum live language-server processes per Hermes process. `0` = unlimited (the pre-2026-11-11 behaviour). |

Both are **resource** bounds. Neither relaxes workspace trust.

The `lsp.enabled`, `lsp.idle_timeout`, `lsp.exclude_roots` and
`lsp.trusted_workspaces` keys are unchanged by this work.

> **`memory.high` is deliberately NOT raised.** The gateway's
> `MemoryHigh` was raised twice on 2026-11-11 (900M → 1200M at 00:45,
> 1200M → 1800M at 03:55) and the working set grew to meet it **both times**,
> because the growth source was unbounded and live. A third bump buys an hour,
> not a fix. The override lives at
> `~/.config/systemd/user/hermes-gateway.service.d/00-memory.conf`
> (`MemoryHigh=1800M`, `MemoryMax=2400M`). **Re-evaluating it downward is a
> separate, measurement-gated step** — owned by `t_5ae9b5c8` — and must not
> happen before this fix is active and the cgroup is measured clean.

## Troubleshooting

### Symptom: several pyright processes alive for one profile

```bash
ps -eo pid,ppid,rss,etime,args | grep -E 'langserver' | grep -v grep
```

More than one pyright **for the same profile** means the shared-key fix is not
active in that process — the gateway (or the worker) imported
`agent/lsp/manager.py` before the change. Restart the gateway (human-gated,
`t_33a986c1`) and re-check.

Distinguish *several pyright* from *one pyright per profile*: the fleet
legitimately runs one per profile. The leak was **two for the same profile**,
which the shared key makes impossible.

### Symptom: the gateway is pinned at `memory.high` and `memory.events: high` climbs

```bash
C=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service
cat "$C/memory.current" "$C/memory.high"; grep '^high' "$C/memory.events"
```

This is the thrashing state that precedes a watchdog kill. Do **not** raise
`memory.high` — the working set will simply grow to meet it. Find what is
holding the memory first (the language servers, or see the sibling
[[Homelab/10-Services/Services/worker-memory-budget|Worker Memory Budget]] for
the host-level accounting).

### Symptom: the gateway exits 75 and the supervisor restarts it

```bash
journalctl --user -u hermes-gateway.service --since '-24h' --no-pager | grep -E 'exiting with code 75|missed 3 consecutive'
```

**This is the gateway's own event-loop liveness watchdog**
(`gateway/shutdown_watchdog.py`), not the systemd watchdog. `WatchdogSec=900`
had **0** timeouts. The watchdog fires when the loop misses three consecutive
liveness probes — which memory pressure makes fatal. Counting firings: grep
for `exiting with code 75`, NOT for `shutdown_watchdog` — the latter also
matches **stack-trace file paths** inside a single dump, which inflates the
count by an order of magnitude.

### Symptom: pyright still grows past `max_server_heap_mb`

`--max-old-space-size` caps the V8 **old space**, not total RSS. Node's
baseline plus the heap still land above the configured MiB. The ceiling is a
GC trigger, not a hard RSS limit.

### Symptom: diagnostics stop appearing after the client cap is hit

The LRU client was evicted and is respawned on demand — the next request for
that workspace re-spawns the server (a moment of latency). Raise
`lsp.max_clients` at the cost of one more server per Hermes process.

### Symptom: a worker touches two repos and expects two servers

It now gets one. That is the fix. The shared untrusted client attaches the
second root via `workspace/didChangeWorkspaceFolders` and serves both. If the
second root needs the *project's* venv (not the operator's), that root must be
**trusted** — add it to `lsp.trusted_workspaces`, and it gets its own
trust-pinned client.

## Dependencies

- `agent/lsp/manager.py` — `_client_key`, `UNTRUSTED_SHARED_ROOT`,
  `_live_key`, `_get_or_spawn`, `_enforce_client_cap`.
- `agent/lsp/servers.py` — `UNTRUSTED_SAFE_SERVERS`, `_spawn_pyright`
  (the trust->interpreter pin), `apply_node_heap_cap`, `ServerContext`.
- `agent/lsp/workspace.py` — `operator_workspace_roots` (the no-anchor rule
  that makes every worker root untrusted).
- `agent/lsp/eventlog.py` — the spawn/reap/release log lines.
- `hermes_cli/config_defaults.py` — the `lsp.max_server_heap_mb` and
  `lsp.max_clients` defaults.
- `gateway/shutdown_watchdog.py` — the liveness watchdog that the memory
  pressure trips.
- `tests/agent/lsp/test_resource_bounds.py` — the regression suite.

## See Also

- [[Homelab/10-Services/Services/worker-memory-budget|Worker Memory Budget]]
  (`t_8e8ec4de`) — the **host**-level overcommit fix, and the page that first
  identified the no-operator-anchor mechanism. This page is the
  **gateway-cgroup** layer: the same servers, the same mechanism, but the
  consequence is the gateway killing itself.
- `docs/wiki/services/duplicate-tool-call-ids.md` — the other live-branch wiki
  page.
- `t_6b5fe961` — moves workers and their language servers **out of** the
  gateway cgroup entirely (a separate cgroup-isolation change). This page
  reduces the footprint; that card removes the coupling.
- `t_5ae9b5c8` — the gateway's watchdog kill mechanism, and the owner of the
  `memory.high` re-evaluation.
- `t_1915c9a3` — guard the gateway self-restart so a restart does not kill
  in-flight cards.
- `t_33a986c1` — the human-gated gateway restart that activates this fix.
- `t_67049b90` — the sibling card that added the two resource bounds.
- `t_2dfa12f1` — this change.
