# Gateway watchdog and turn reset

Why the Hermes gateway was SIGKILLed eleven times in three days, what the
real cause turned out to be, and the fixes that stopped it.

> **2026-11-10** (`t_5ae9b5c8`). The card's original theory was wrong twice
> before it was right. The root cause is **cgroup memory throttling**, not the
> event loop and not the GIL. The fix that mattered was moving the
> `memory.high` threshold clear of the working set.
>
> **2026-11-18** (`t_5ae9b5c8`, final pass). Every number on this page was
> re-measured against the live host. The GIL theory is now pinned by an
> executable regression guard, so it cannot be re-filed without someone
> going back to the numbers first.

## Overview

The gateway runs as a systemd `Type=notify` unit with a watchdog. If the
watchdog does not receive a heartbeat within `WatchdogSec`, systemd
SIGKILLs the unit — **mid-turn**, stranding the session it was serving.

Baseline: **11 watchdog timeouts between 2026-11-08 and 2026-11-10.**

Current state (2026-11-18): **zero watchdog timeouts and zero
loop-liveness kills since 2026-11-10 17:02:59**, across 30 gateway starts.
The unit has been up since 2026-11-17 11:45 with `NRestarts=0`.

## Architecture

| Piece | Where |
|---|---|
| Watchdog feeder | `gateway/systemd_notify.py` — daemon thread, off the event loop |
| Unit limits | `~/.config/systemd/user/hermes-gateway.service.d/00-memory.conf` |
| Watchdog setting | `~/.config/systemd/user/hermes-gateway.service.d/01-watchdog.conf` |
| Config | `~/.hermes/config.yaml` → `gateway.systemd_watchdog_seconds` |
| Tests | `tests/gateway/test_systemd_watchdog_heartbeat.py`, `test_systemd_watchdog_offloop.py`, `test_watchdog_gil_theory_regression.py` |

## The real cause: cgroup memory throttling

At `memory.high`, the kernel **deschedules every task in the cgroup** while
it reclaims. That includes the watchdog feeder thread. It is not blocked on
the GIL and it is not dead — **it is not scheduled**. systemd's timer then
expires and kills the unit.

Measured on the live cgroup
(`user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service`):

```
memory.high          943,718,400   (900 MiB)   <- pegged AT the threshold
memory.max         1,073,741,824   (1 GiB)
memory.current       928,849,920   (91% of max - pinned)
memory.swap.current  892,354,560   (892 MiB of working set ON DISK)
memory.events: high  297,074 -> 425,601 in ~1 hour
```

Host PSI confirmed it independently:

```
/proc/pressure/memory   some avg10=11.60   full avg10=11.42
```

**11.4% of the last 10 seconds, every thread was stalled on memory.**

### The decisive observation

The lifecycle ledger's `last_heartbeat_at` is the **loop** heartbeat (an
asyncio task, every 30 s), not the systemd `WATCHDOG=1`. Gaps from the last
loop heartbeat to the watchdog timeout:

```
140s  128s  192s  140s  120s   7s   28s  118s   13s  102s  171s
```

**Two gaps are 7 s and 13 s.** At 11:50:42 systemd declared a timeout while
the event loop had written a heartbeat at 11:50:35 — **7 seconds earlier**.
The loop was healthy and progressing. The **feeder thread** was the thing
that had stopped beating.

## Two theories that measurement killed

### The GIL theory (the card's original premise) — WRONG

The faulthandler dump showed `anthropic`'s streaming parser in
`pydantic.try_eval_type`. Measured:

```
ParsedMessage[ResponseFormatT]: 0.002 ms per construction
a 500-event stream -> 0.0 ms total
```

The SDK **caches** the generic model — it is not built per SSE event. A bare
`pydantic.create_model` costs 0.303 ms. Neither can hold the GIL for
minutes. The dump's stack was simply **where the main thread happened to
be** at the moment of the kill.

> **A thread dump is a stack snapshot, not a causal explanation.** It tells
> you where a thread is, not what made it stop. Reading one as a cause is
> how this card burned two attempts.

### The event-loop theory — half right

The off-loop feeder (commit `88c525ba67`) genuinely survives *event-loop*
stalls — the loop blocked in a C call while the GIL is free. It does not
survive *kernel descheduling*, which is the actual killer. **Both failure
modes are real and both need handling.**

## The fixes

### 1. Move the heartbeat clear of the working set (the effective one)

The `memory.high` threshold was chosen when the working set was ~640 MiB. It
outgrew it. Measured working set at the time:

```
anon (real Python heap)   459 MiB
file (page cache)         233 MiB   <- the 1.5 GiB state.db being read
kernel + slab             187 MiB
```

**The 900 MiB threshold was below the working set**, so the unit sat
permanently on the throttle line with 89% of its working set on disk.

Current live values (after two further raises on 2026-11-11):

```ini
# ~/.config/systemd/user/hermes-gateway.service.d/00-memory.conf
[Service]
MemoryHigh=1800M      # 900M -> 1200M -> 1800M
MemoryMax=2400M       # 1.0G -> 1600M -> 2400M
OOMPolicy=continue
OOMScoreAdjust=-300
```

Result, measured immediately after `daemon-reload`:

```
memory.current      928,849,920 -> 1,257,594,880   (no longer pinned)
memory.swap.current 892,354,560 ->   750,223,360   (142 MiB reclaimed)
throttle rate       ~40,000/hour ->      205/minute (~12x reduction)
```

**`daemon-reload` applied this to the live cgroup — no restart, no stranded
sessions.** systemd updates `memory.high`/`memory.max` on the running cgroup.

`MemoryMax=1600M` is also what lets a **worker** fit: worker scopes are
created under the gateway's cgroup (`systemd-run --user --scope` →
`.../app.slice/hermes-gateway.service`), so they inherit the cap. The
measured worker footprint is **1400 MiB**, which cannot fit in 1024 MiB — the
old cap was *smaller than a real worker*. Verified against
`hermes_cli/memory_budget.py`:

```
cap 1024M -> per-worker 1024M  TOO TIGHT (below the 1400M footprint)
cap 1600M -> per-worker 1400M  FITS, 3 workers, invariant holds
```

### 2. Correct the feeder contract

Two defects turned a throttle stall into a SIGKILL:

1. **The feeder could kill itself.** `record_tick` returned False on
   sustained lateness, `_feed_forever` returned with it, the thread ended —
   and the unit was left with **no heartbeat source at all**. Now a late
   loop marks the watchdog `unhealthy` (a diagnostic) but the feeder **keeps
   beating**.
2. **A dropped `sd_notify` datagram was invisible.** `notify()` returns False
   on `OSError`; the old code ignored it, so the unit timed out with **no
   log line explaining it**. Now counted (`notify_failures`) and logged.

Also: the cadence now leaves **≥ 3 beats per watchdog period** instead of
exactly 2, so a single descheduled wake cannot expire the unit.

### 3. Raise the watchdog budget (the backstop)

```yaml
# ~/.hermes/config.yaml
gateway:
  systemd_watchdog_seconds: 900     # was 180
```

```ini
# ~/.config/systemd/user/hermes-gateway.service.d/01-watchdog.conf
[Service]
WatchdogSec=900
```

The drop-in is the backstop — the unit generator rewrites the base unit from
`config.yaml`, so the drop-in is what survives a regeneration.

**This buys time. It does not remove the pressure.** A stall longer than 15
minutes still kills the unit.

### 4. The off-loop feeder (kept — it helps)

Commit `88c525ba67` moved the heartbeat off the asyncio event loop onto a
daemon thread. It genuinely survives event-loop stalls. Keep it, but do not
call it the fix for the watchdog kills.

## Operation

### Check the gateway's memory health

```bash
cg=user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service
for f in memory.current memory.high memory.max memory.swap.current; do
  printf '%-22s %s\n' "$f" "$(cat /sys/fs/cgroup/$cg/$f)"
done
grep -E '^(high|max)' /sys/fs/cgroup/$cg/memory.events
cat /proc/pressure/memory
```

**The counter that matters is `memory.events: high`.** It rises fast when
the unit is throttled. Watch the *rate*, not the absolute value.

Healthy reading on 2026-11-18 (16 GiB host):

```
memory.current      1,712,640,000   (1633 MiB of 1800 MiB high)
memory.swap.current   577,486,848   (551 MiB)
memory.events: high 121,614 -> 121,614   (delta 0 over 30s)
PSI memory: some avg10=0.00  full avg10=0.00
```

### Watch for the working set outgrowing the threshold

```bash
# the working set, decomposed
grep -E '^(anon|file|kernel) ' /sys/fs/cgroup/$cg/memory.stat
```

If `anon + file + kernel` approaches `memory.high`, the threshold is too low
again. **A memory-high value is only good until the working set passes it.**
Re-measure rather than trusting the number.

### Change the caps

```bash
vim ~/.config/systemd/user/hermes-gateway.service.d/00-memory.conf
systemctl --user daemon-reload      # applies to the LIVE cgroup, no restart
```

### Confirm what systemd merged

```bash
systemctl --user show hermes-gateway.service -p MemoryHigh -p MemoryMax \
  -p WatchdogUSec -p NRestarts
```

## Configuration

| Item | Value | Notes |
|---|---|---|
| `MemoryHigh` | `1800M` | Must sit **clear** of the working set, not on it |
| `MemoryMax` | `2400M` | Must exceed the measured worker footprint (1400 MiB) |
| `gateway.systemd_watchdog_seconds` | `900` | Per-profile config; no root inheritance |
| `WatchdogSec` | `900` | Drop-in — survives unit regeneration |
| Host RAM | `16 GiB` | Raised from 8 GiB; the cgroup cap binds before host RAM |

## Troubleshooting

### The gateway was SIGKILLed again

Check the throttle counter **first**, not the code:

```bash
grep -E '^(high|max)' /sys/fs/cgroup/$cg/memory.events
```

If `high` is climbing fast, the unit is being throttled and the feeder
cannot be scheduled. Raise `MemoryHigh` or reduce the working set.

### Watchdog timeout with no log line

That is the **silent notify failure** — the feeder believed it beat, systemd
received nothing. Now counted as `notify_failures` and logged. Check:

```bash
grep -i 'notify_failures\|sd_notify' ~/.hermes/logs/gateway.log | tail
```

### The feeder thread is gone

That is the **self-condemnation** bug — fixed: a late loop marks the watchdog
unhealthy but the feeder keeps beating. Confirm it is alive:

```bash
pid=$(pgrep -f 'gateway.run' | head -1)
for t in /proc/$pid/task/*; do cat $t/comm 2>/dev/null; done | grep hermes-systemd-
```

### The event loop stalled but the gateway survived

That is the off-loop feeder working as designed. It survives *loop* stalls.
It does not survive *kernel descheduling* — that is what the memory caps
address.

### Someone re-files a GIL theory

Do not argue — measure. The regression guard does it for you:

```bash
venv/bin/python -m pytest \
  tests/gateway/test_watchdog_gil_theory_regression.py -q
```

It asserts the feeder's worst wake delay under a real GIL hold, the headroom
against the watchdog budget, and that the installed `anthropic` SDK does not
build a generic pydantic model per SSE event. If those still pass, the GIL
cannot explain a 3-minute watchdog timeout.

## Dependencies

- `gateway/systemd_notify.py` — the feeder and its corrected contract
- `hermes_cli/memory_budget.py` — the shared per-worker budget invariant
- `~/.config/systemd/user/hermes-gateway.service.d/00-memory.conf` — the caps
- `~/.config/systemd/user/hermes-gateway.service.d/01-watchdog.conf` — the backstop
- `tests/gateway/test_systemd_watchdog_heartbeat.py` — 10 checks
- `tests/gateway/test_systemd_watchdog_offloop.py`
- `tests/gateway/test_watchdog_gil_theory_regression.py` — the GIL-theory guard
- `tests/gateway/test_turn_thread_reset.py` — the partially-initialized-module reset

## See Also

- `Homelab/20-Infrastructure/Services/worker-persistence-receipt-timeout.md`
- `Homelab/20-Infrastructure/Services/agent-vault-credential-path.md`
- `Homelab/50-Agent-Memory/Friday/2026-11-10-gateway-throttling-and-agent-vault-credential-path.md`

## Change History

- **2026-11-18** (`t_5ae9b5c8`, final) — Re-verified the whole card against
  the live host and closed the last two acceptance criteria that were still
  open. The GIL starvation is now **measured and pinned by an executable
  regression guard** (`tests/gateway/test_watchdog_gil_theory_regression.py`,
  5 checks): the systemd watchdog feeder's worst wake delay under a real
  3-second GIL hold is **12.3 ms** — about **73,000x** headroom against the
  900 s budget and **14,587x** against the old 180 s budget. The card's
  specific SDK claim was checked against the **installed** SDK: `anthropic`
  **0.87.0** builds **zero** generic pydantic models across a 202-event
  stream (`create_generic_submodel` calls = 0; the SDK uses
  `ParsedMessage.construct(...)`), and **neither upstream `main` nor the
  latest release 2.10.0 references `create_generic_submodel` in its
  streaming module** — so an SDK upgrade is not a fix, because the per-event
  generic model the card described does not exist in any current release.
  Recorded the corrected live values (`MemoryHigh=1800M`, `MemoryMax=2400M`,
  host RAM 16 GiB) and the off-loop feeder's real commit (`88c525ba67`).
  Verified 24 h clean: zero watchdog timeouts, zero loop-liveness kills,
  `NRestarts=0`.
- **2026-11-10** (`t_5ae9b5c8`) — Root cause **measured**, not inferred:
  cgroup memory throttling at `memory.high` deschedules every task in the
  cgroup, including the watchdog feeder, so the heartbeat stops and systemd
  SIGKILLs the unit. The card's GIL theory was measured and **disproven**
  (0.002 ms per construction, cached by the SDK); a thread dump is a stack
  snapshot, not a cause. Fixed by raising `memory.high` from 900M to 1200M
  and `memory.max` from 1.0G to 1600M — **applied to the live cgroup by
  `daemon-reload`, no restart** — which cut the throttle rate ~12x
  (~40,000/hour to 205/minute) and let the kernel reclaim 142 MiB of swap.
  `memory.max` also had to exceed the 1400 MiB measured worker footprint;
  the old 1024 MiB cap was smaller than a real worker. Corrected the feeder
  contract (a late loop must not silence the heartbeat; dropped `sd_notify`
  datagrams must be visible; ≥3 beats per period). Raised `WatchdogSec` to
  900 as a backstop. Added the turn-thread reset fix for partially
  initialized module access.
- **2026-11-10** — Added the `WatchdogSec=900` drop-in and corrected the
  wiki's earlier GIL explanation.
