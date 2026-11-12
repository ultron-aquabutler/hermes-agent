# Managed Workers in Their Own cgroup (t_6b5fe961)

> Wiki page for the managed-worker cgroup isolation fix.
>
> Status: **COMMITTED AND LIVE.** The managed workers now run in
> `hermes-worker-local-*.scope` units instead of the gateway's own cgroup.
> Verified live on 2026-11-12 against gateway pid 3993120.

## Overview

`agent.managed_worker` is the process that runs a kanban or cron card's turn.
Before this change it was spawned with a plain `subprocess.Popen` and no
cgroup redirection, so it **inherited the gateway's cgroup**. Every worker
therefore counted against the gateway's `memory.high` / `memory.max`, and the
gateway could be throttled to death by work it was merely supervising.

The `hermes-worker-*.scope` units that appeared in `systemctl --user list-units`
made the topology *look* correct, but they wrapped only the **submitter**. The
real worker sat in `hermes-gateway.service`. A scope unit's name is not proof
of what is inside it — always read `/proc/<pid>/cgroup` for the actual process.

## Architecture

### The spawn path

```
gateway/session_managed_worker.py
  -> _managed_worker_scope(session_id, execution_id)   # builds the unit suffix
  -> gateway/session_worker_scope.py::managed_worker_scope()
  -> systemd-run --user --scope <unit>
  -> subprocess.Popen([sys.executable, '-m', 'agent.managed_worker'], ...)
```

`gateway/session_worker_scope.py` is the new module that owns the scope
creation and the fallback path.

### The fallback is degraded isolation, never a dropped worker

If the user D-Bus session is unavailable, the scope cannot be created. The
worker **must still run** — it simply stays in the gateway's cgroup. A failed
worker is worse than an isolated-but-unisolated worker: the card loses its
turn, and the dispatcher burns retries.

### The two things this card did NOT do

- It did **not** raise `memory.high`. `MemoryHigh` was raised twice already
  (`900M -> 1200M -> 1800M`) and the working set met each new value within
  the hour, because the growth source was live and inside the gateway's cgroup.
- It did **not** introduce a second per-worker memory number. The cap comes
  from the shared `hermes_cli/memory_budget.py::per_worker_memory_max_bytes()`.

### The sibling fix: commands that share the gateway's budget

`tools/process_registry.py` gained `_shares_gateway_cgroup()` (commit
`ce1fb45d32`). A local executor spawned by the gateway — Codex, pytest, Node
— inherits the gateway's cgroup, so its memory counts against the gateway's
budget even though it is not a gateway process. That detection routes those
commands into a scope instead.

## Operation

### Verify the topology on the live tree

```bash
for p in $(pgrep -f 'agent.managed_worker'); do
  printf "%s " "$p"
  awk -F: '/^0::/{print $3}' /proc/$p/cgroup
done
```

A correct worker shows a `hermes-worker-local-*.scope` path. A worker showing
`hermes-gateway.service` is still unisolated.

### Count what the gateway cgroup actually holds

```bash
CG=$(systemctl --user show hermes-gateway.service -p ControlGroup --value)
for p in $(cat /sys/fs/cgroup$CG/cgroup.procs); do
  printf "%-9s %8s KiB  %s\n" "$p" \
    "$(awk '/VmRSS/{print $2}' /proc/$p/status)" \
    "$(tr '\0' ' ' < /proc/$p/cmdline | cut -c1-60)"
done | sort -k2 -n -r
```

Before the fix: 33 processes, 6 managed workers holding ~1.7 GB.
After the fix: 7 processes.

### Watch the gateway's memory headroom and throttle rate

```bash
CG=$(systemctl --user show hermes-gateway.service -p ControlGroup --value)
awk '{printf "current=%.0f MiB\n", $1/1048576}' /sys/fs/cgroup$CG/memory.current
grep high /sys/fs/cgroup$CG/memory.events
```

### Run the regression tests

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python3 -m pytest tests/gateway/test_managed_worker_cgroup_isolation.py -q
```

14 passed on 2026-11-12.

## Configuration

- `MemoryHigh=1800M`, `MemoryMax=2400M` in
  `~/.config/systemd/user/hermes-gateway.service.d/00-memory.conf`.
- The per-worker `MemoryMax` is derived from `hermes_cli/memory_budget.py`,
  not configured separately.

## Troubleshooting

### Symptom: the gateway is pinned at `memory.high` and `memory.events: high` climbs

**Check the cgroup composition before assuming it is a heap leak.** The
gateway cgroup holds *page cache* as well as heap, and `memory.high`
throttles the cgroup when `current > high` **even for reclaimable pages**:

```bash
CG=$(systemctl --user show hermes-gateway.service -p ControlGroup --value)
awk '/^anon |^file |^kernel /{printf "%s %.0f MiB\n", $1, $2/1048576}' \
  /sys/fs/cgroup$CG/memory.stat
```

Measured 2026-11-12 on gateway pid 3993120:

```
anon   807 MiB   (heap)
file   877 MiB   (page cache - 615 MiB of it INACTIVE and reclaimable)
kernel 115 MiB
current 1799.5 MiB  ==  memory.high 1800 MiB
```

The host had 11.6 GB available. The gateway was throttling itself against a
ceiling it was not actually consuming.

**The fix needs no restart.** Ask the cgroup to reclaim its own cache:

```bash
CG=$(systemctl --user show hermes-gateway.service -p ControlGroup --value)
echo $((900*1024*1024)) > /sys/fs/cgroup$CG/memory.reclaim
```

That dropped `current` from **1800 MiB -> 921 MiB** (file cache 841 -> 115 MiB)
with the gateway still running, the dashboard still serving HTTP 200, and the
worker scopes intact.

### Symptom: a managed worker still shows the gateway cgroup

The scope was not created — almost always because the user D-Bus session is
unavailable. Check that `systemd-run --user --scope` works from the gateway's
session. The worker should still run (degraded isolation).

### Symptom: `memory.current` is at `memory.high` but `memory.stat` shows little anon

It is page cache, not heap. See the first symptom above. Do **not** raise
`memory.high` — that was tried twice and the working set met each new value.

### Symptom: the framed result handshake fails after a scope change

The gateway holds the child's stdin/stdout and reads a framed result
(`agent/managed_worker.accept_result` / `encode_frame` / `read_frame`). A
scope wrapper changes the process topology, so the framing must survive it.
`tests/gateway/test_managed_worker_cgroup_isolation.py` pins this end to end.

### Symptom: the gateway exits 75 and the supervisor restarts it

Check whether the provider was rate-limiting, not the worker. A provider
429 surfaces as `exit 75` and the text "usage limit reached" — that text can
be *inherited* in `last_failure_error` from an earlier run and does not mean
the current run hit a quota wall.

## Dependencies

- `gateway/session_worker_scope.py` — scope creation and fallback.
- `gateway/session_managed_worker.py` — the spawn path.
- `tools/process_registry.py` — `_shares_gateway_cgroup()` for gateway-spawned
  local executors (`ce1fb45d32`).
- `hermes_cli/memory_budget.py` — the shared per-worker memory cap.
- `systemd-run --user --scope` and an available user D-Bus session.

## See Also

- [[lsp-gateway-cgroup-footprint]] — the language servers were the other
  gateway-cgroup resident, and the larger holder.
- `Homelab/50-Agent-Memory/Friday/2026-11-11-dispatcher-caps-and-memory-ceiling.md`
  — the dispatcher false-positive and the `memory.high` sizing history.
