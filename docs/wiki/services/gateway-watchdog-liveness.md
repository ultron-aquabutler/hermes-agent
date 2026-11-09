# Hermes Gateway — Watchdog Liveness and Loop-Wedge Detection (t_dd79272f)

> Wiki page for the off-loop systemd watchdog feeder and the loop-wedge detector.
>
> Status: **DEPLOYED on the hub gateway** (see "Deployment evidence" below).
> The code lives in commit `d62445ee21` plus the reconciliation in this branch
> (`fix/t_683c3c25-watchdog-feeder`), based on `canonical-deploy-t_7161d9a9`.
>
> The live operator-facing page — which covers the whole gateway, not just the
> watchdog — is the Obsidian page
> `Homelab/20-Infrastructure/Hermes/hermes-gateway.md`. This page is the
> git-tracked counterpart that ships with the code it describes.

## Overview

The Hermes gateway is supervised by systemd with `WatchdogSec=180s`. The
gateway must send `WATCHDOG=1` at least twice per watchdog period, or systemd
reclaims the unit and restarts it.

That produced a coupling bug on 2026-11-03. The heartbeat was produced by an
`asyncio` task on the gateway's **main event loop**, so the watchdog's own
liveness signal depended on the very thing it was monitoring:

```
00:09:26   last loop heartbeat
00:12:07   systemd: Watchdog timeout (limit 3min)! Killing <pid> with SIGABRT
00:12:08   Fatal Python dump: "Current thread 0x... [hermes]: Garbage-collecting"
```

The main thread was in `Garbage-collecting`, so the loop could not run a single
callback, so the heartbeat task could not run, so `WATCHDOG=1` stopped, so
systemd SIGABRTed a gateway that was about to recover on its own.

**What is NOT established:** the faulthandler snapshot is a single instant. It
does not prove GC occupied the whole 161-second window, and it does not identify
what allocated the objects being collected. The *mechanism* and the *defect* are
established; the trigger is not.

The fix moves the heartbeat off the loop onto a dedicated feeder thread, and
adds a failure-reporting layer so a feeder that dies is never silent. This
page documents what that changes, what it does not change, and the one
residual risk it introduces.

## Deployment evidence — the gap measurement

The card this fix came from (`t_683c3c25`) originally blamed a CPython 3.14
native abort. It was a misattribution: the "Fatal Python error: Aborted" dump
and the `posixpath.realpath` / `_drain_fd_select` frames were the faulthandler
snapshot of whichever threads happened to be running when systemd's signal
landed. They are the victim, not the cause.

The cause is visible in the gateway's own lifecycle ledger
(`~/.hermes/logs/gateway-exit-diag.log`), which records the last **loop**
heartbeat before every unclean exit. For all twelve crashes in the window:

| Death captured | Last loop heartbeat | Gap |
|---|---|---|
| 2026-11-03T00:13:05 | 2026-11-03T00:09:26 | 218.8 s |
| 2026-11-04T00:20:49 | 2026-11-04T00:18:31 | 138.4 s |
| 2026-11-04T19:48:59 | 2026-11-04T19:46:35 | 143.5 s |
| 2026-11-07T23:03:43 | 2026-11-07T23:00:48 | 175.6 s |
| 2026-11-07T23:14:27 | 2026-11-07T23:11:18 | 189.0 s |
| 2026-11-07T23:24:40 | 2026-11-07T23:22:12 | 147.5 s |
| 2026-11-07T23:41:25 | 2026-11-07T23:38:15 | 190.2 s |
| 2026-11-07T23:58:23 | 2026-11-07T23:55:36 | 167.4 s |
| 2026-11-08T00:19:32 | 2026-11-08T00:16:39 | 172.6 s |
| 2026-11-08T20:47:28 | 2026-11-08T20:44:44 | 164.4 s |
| 2026-11-08T22:07:09 | 2026-11-08T22:03:23 | 225.4 s |
| 2026-11-08T22:27:45 | 2026-11-08T22:24:49 | 175.8 s |

**min 138.4 s, max 225.4 s, mean 175.7 s** — clustered on the 180 s
`WatchdogSec`. Two of the twelve exceed 180 s because the ledger samples the
death at the *next* gateway start, after the restart delay; the true
beat-stop-to-SIGABRT interval is therefore ≤ the measured gap.

Every crash shows the same shape: the loop heartbeat stops, and ~180 seconds
later systemd reclaims the unit. That is the loop-driven feeder being silenced
by the very stall it was supposed to survive. The `cgroup_memory_events` in
every one of these records show `oom_kill: 0`, so this is not memory pressure.

Reproduce the contrast against the deployed file (`md5 38a1ccc09be9221bb7cd9c671263f973`):

```
AssertionError: the systemd watchdog feeder stopped beating while the event loop
was stalled; systemd would SIGABRT the unit 180s after the last beat.
beats=0 messages=['STATUS=watchdog unhealthy: event loop progress is late']
```

`beats=0`, and the only datagram sent was the feeder's own death notice. That
is the production signature exactly.


## Architecture

Two contracts, deliberately separated:

| Question | Answered by | Mechanism |
|---|---|---|
| Is the **process** still making progress? | systemd `WatchdogSec=180s` | the off-loop feeder thread (`SystemdWatchdog._feed_forever`) sends `WATCHDOG=1` on a fixed cadence, independent of the event loop |
| Is the **event loop** still making progress? | the off-loop loop-liveness watchdog | `gateway/shutdown_watchdog.py::start_loop_liveness_watchdog` probes the loop via `call_soon_threadsafe` and escalates to a faulthandler dump plus exit 75 after sustained misses |

The systemd backstop is **not** weakened. A process holding the GIL cannot run
*any* Python thread, so it cannot run the feeder either — the beat stops exactly
as before and systemd reclaims the unit.

### The feeder reports its own death

An off-loop feeder that dies is the same silent-death class the off-loop feeder
was built to fix: Python prints nothing when a thread ends, so a feeder that
died left the gateway running and logging as if nothing were wrong while
systemd's timer counted down to the SIGABRT.

The feeder therefore has a **health surface** that is a statement about the
FEEDER, never about systemd:

| Property | Meaning |
|---|---|
| `healthy` | True only when the feeder is running AND actually delivering beats. Strict: False when the feeder ended, when it is not feeding, or when sends are failing. |
| `failed` / `failure_reason` | Set when the feeder ended for a reason nobody asked for. `failure_reason` names it: the exception type, or `returned: the feeder thread returned without being stopped`. |
| `heartbeat_failures` | Count of beats whose datagram did not go out. Reset on the first success. |
| `consecutive_missed_beats` | Consecutive beats that produced nothing. This is the state that must never be reported as healthy. |
| `feeder_alive` | Whether the feeder thread is running right now. |
| `last_heartbeat_monotonic` | When the last beat actually went out. |

A successful send means "the datagram was handed to the kernel" — **never**
"systemd accepted it". `sd_notify` is fire-and-forget and systemd's manager is
the only authority on acceptance. No attribute on the health surface claims
otherwise, and a test enforces that (`test_health_never_claims_systemd_accepted_the_datagram`).

When the feeder dies, the gateway:

1. logs at **CRITICAL** with the reason — the operator can read WHY in the
   journal instead of inferring it from the absence of evidence;
2. stamps the runtime status `gateway_state=degraded`,
   `exit_reason=systemd_watchdog_feeder_failed`, so the dashboard shows the
   gateway is unsupervised **180 s before** systemd kills it;
3. does that stamp **off the event loop** and without blocking —
   `write_runtime_status` takes a process-wide lock and then blocks on the
   background writer (measured 2.5 s of stall with a slow writer, in a gateway
   whose whole problem is stalls).

A missing `NOTIFY_SOCKET` is also warned about, separately from a failed send:
a silent no-op there is indistinguishable from a healthy heartbeat, which is
exactly how a dead feeder hides.

### The loop-liveness watchdog is now load-bearing

This is the important consequence, and it was measured rather than assumed.

`tests/gateway/test_loop_wedge_contrast.py` runs a **real** asyncio event loop
in a dedicated thread and then genuinely stops that thread from running the
loop — the faithful stand-in for "the main thread is in `Garbage-collecting`".
Under that identical stall:

| Implementation | Behaviour during the stall | Consequence |
|---|---|---|
| **BASE** (pre-fix, loop-driven heartbeat) | beats STOP | systemd's `WatchdogSec` expires → unit reclaimed (SIGABRT) |
| **FIX** (off-loop feeder) | beats CONTINUE | systemd does NOT reclaim; only the loop-liveness watchdog detects the wedge |

So before the change, a stuck loop was caught by systemd — bluntly, with a
SIGABRT and no diagnosis of why the loop was stuck. After the change, it is
caught by the loop-liveness watchdog, which dumps every thread's stack and
exits 75. That is a **better** outcome (it diagnoses the cause), but it means
the loop-liveness watchdog is now the *only* detector for loop wedges.

### Failure modes, and what catches each

| Failure mode | Detected by | Notes |
|---|---|---|
| Main thread in a long GC pass / busy loop | loop-liveness watchdog (off-loop probe thread) | the feeder keeps systemd fed; the probe thread misses and escalates |
| Process wedged holding the GIL (long C call) | systemd `WatchdogSec` | the feeder cannot run either — the beat stops, systemd reclaims the unit |
| Loop idle but something else wrong | faulthandler dump shows the main thread parked in `selectors.select` | investigate the other threads: `gateway-turn-*`, `session-db-*`, `cron-parallel_*` |
| **Loop-liveness watchdog DISABLED** (`gateway.loop_watchdog: false`) | **nothing in-process** | see the residual risk below |

### Timing budget

| Knob | Default | Where |
|---|---|---|
| `gateway.systemd_watchdog_seconds` | 180 | `~/.hermes/config.yaml`; `WATCHDOG_USEC` is half of it, and the feeder beats at half of *that* (45 s) |
| `gateway.loop_watchdog_probe_interval_s` | 30.0 | `gateway/shutdown_watchdog.py` |
| `gateway.loop_watchdog_probe_timeout_s` | 10.0 | `gateway/shutdown_watchdog.py` |
| `gateway.loop_watchdog_max_strikes` | 3 | `gateway/shutdown_watchdog.py` |

Worst case for loop-wedge detection: `probe_interval + probe_timeout`, then
`max_strikes` consecutive misses — roughly 120 s before exit 75. That is
comfortably inside the 180 s systemd window, so the loop detector normally wins
the race.

## Operation

### Verify the live gateway is running the fix

The deployed tree carries the fix. Check which code is actually loaded:

```bash
# The FIXED implementation has an off-loop feeder thread.
grep -n "_feed_forever" /home/serveradmin/.hermes/hermes-agent/gateway/systemd_notify.py

# The FIXED implementation also has the health surface.
grep -n "def healthy\|failure_reason" /home/serveradmin/.hermes/hermes-agent/gateway/systemd_notify.py

# The running process's threads: the feeder thread is named
# "hermes-systemd-watchdog" when the fix is deployed.
for t in /proc/$(systemctl --user show hermes-gateway -p MainPID --value)/task/*; do
  printf '%s ' "$(cat "$t/comm" 2>/dev/null)"
done; echo
```

If `grep` finds nothing, the deployed tree is the OLD code.

### Verify the watchdog heartbeat is advancing

```bash
systemctl --user show hermes-gateway \
  -p Type -p NotifyAccess -p WatchdogUSec -p MainPID -p NRestarts \
  -p ActiveState -p Result
# Expected: Type=notify, NotifyAccess=main, WatchdogUSec=3min,
# ActiveState=active, Result=success

systemctl --user show hermes-gateway -p WatchdogTimestamp --value
# Take another sample after MORE THAN 45 seconds; the timestamp must advance.
```

Do not diagnose a frozen watchdog from samples taken less than 45 seconds
apart. `WatchdogUSec=0` means the running service has not been restarted since
the unit changed, even if the on-disk unit already says `Type=notify`.

**`NRestarts` is the metric that matters.** It is the count of times systemd
had to reclaim the unit. After the fix it must not increase.

### Verify the loop-liveness watchdog is armed

The loop detector is off-loop and invisible from the event loop. Confirm it is
enabled in config, and confirm the thread exists in a running gateway's thread
list (the thread is named `gateway-loop-liveness-watchdog`):

```bash
grep -n "loop_watchdog" /home/serveradmin/.hermes/config.yaml
# Expect loop_watchdog: true (the default). If it is false, the residual risk
# below applies and there is no in-process loop-wedge detector at all.

# Thread names for the live gateway PID:
for t in /proc/$(systemctl --user show hermes-gateway -p MainPID --value)/task/*; do
  printf '%s ' "$(cat "$t/comm" 2>/dev/null)"
done; echo
```

### Check whether a crash was a watchdog timeout or something else

Every unclean gateway exit is recorded in the lifecycle ledger with the last
loop heartbeat before the death:

```bash
grep -o '"ts": "[^"]*", "tag": "gateway.previous_unclean_exit".*"last_heartbeat_at": "[^"]*"' \
  /home/serveradmin/.hermes/logs/gateway-exit-diag.log | tail -12
```

The gap between the two timestamps is the diagnostic. A gap clustered around
180 s is a watchdog timeout: the loop stalled, the loop-driven feeder stopped
beating, and systemd reclaimed the unit. A short gap means something else
killed the process. The same records carry `cgroup_memory_events`, so
`oom_kill: 0` rules memory pressure out for that death.

### Run the regression tests

```bash
cd /home/serveradmin/.hermes/hermes-agent
./venv/bin/python -m pytest \
  tests/gateway/test_systemd_notify.py \
  tests/gateway/test_systemd_watchdog_feeder_offloop.py \
  tests/gateway/test_systemd_watchdog_feeder_liveness.py \
  tests/gateway/test_loop_wedge_safety.py \
  tests/gateway/test_loop_wedge_contrast.py \
  tests/gateway/test_loop_liveness_watchdog.py \
  tests/gateway/test_systemd_watchdog_lifecycle.py \
  tests/gateway/test_shutdown_watchdog.py \
  tests/agent/test_kanban_guardrail_block.py
```

**A trap worth knowing:** the venv's editable install pins every Hermes
package to `/home/serveradmin/.hermes/hermes-agent`, so a test run from a
worktree imports the LIVE tree unless the worktree is first on `sys.path`.
Under `pytest` with the worktree as the rootdir the local tree wins; a bare
`python -c "import gateway"` from elsewhere does not. Always confirm which
module a test actually loaded:

```python
from gateway import systemd_notify
print(systemd_notify.__file__)
```

**Another trap:** `WATCHDOG_USEC` is already set in the environment of any
worker spawned BY the gateway, so `os.environ.setdefault("WATCHDOG_USEC", ...)`
in a scratch probe silently keeps the inherited 180 s value. Use
`monkeypatch.setenv` in tests (which overrides) and assign the variable
unconditionally in probes.


## Configuration

| Key | Where | Meaning |
|---|---|---|
| `gateway.systemd_watchdog_seconds` | `~/.hermes/config.yaml` | systemd `WatchdogSec`. `0` disables the watchdog entirely (and drops the unit to `Type=simple`). |
| `gateway.loop_watchdog` | `~/.hermes/config.yaml` | Master switch for the loop-liveness watchdog. Default `true`. **`false` leaves a stuck loop detected by nothing** (see Troubleshooting). |
| `gateway.loop_watchdog_probe_interval_s` | `~/.hermes/config.yaml` | Seconds between loop probes. Default 30.0. |
| `gateway.loop_watchdog_probe_timeout_s` | `~/.hermes/config.yaml` | Seconds a probe may wait for the loop to answer. Default 10.0. |
| `gateway.loop_watchdog_max_strikes` | `~/.hermes/config.yaml` | Consecutive missed probes before the hard exit. Default 3. |
| `HERMES_SYSTEMD_TIMEOUT_START_SEC` | env | Overrides the generated `TimeoutStartSec`. |

## Troubleshooting

### Symptom: the gateway is alive but nothing is happening, and systemd is not killing it

**This is the residual risk of the off-loop feeder, and it is real.**

Before the change, a stuck event loop silenced the heartbeat, so systemd's
`WatchdogSec` reclaimed the unit. After the change the feeder keeps systemd fed
regardless, so systemd will **not** reclaim a gateway whose loop is wedged.
Detection now rests entirely on the off-loop loop-liveness watchdog.

**If `gateway.loop_watchdog: false` is set, there is NO in-process detector.**
The gateway stays up with a dead loop: the cron ticker, the control socket, and
the platform adapters are all frozen, and nothing complains. This is pinned as
an explicit test
(`tests/gateway/test_loop_wedge_safety.py::test_disabled_loop_liveness_guard_leaves_a_stuck_loop_undetected`)
so the risk is visible rather than assumed away.

**Do not set `gateway.loop_watchdog: false` on a production gateway.** The
opt-out exists for platforms where a slow loop is expected and a restart is
worse than the stall; it is not a performance knob.

**The remaining backstop** is the independent
`hermes-gateway-liveness-watchdog.timer` — a sibling systemd oneshot that
checks `~/.hermes/state/gateway.heartbeat` and alerts if it is stale for more
than 300 s while the gateway PID is alive. That heartbeat is written by an
asyncio task, so it freezes with the loop. It **alerts**; it does not restart.

```bash
systemctl --user show hermes-gateway-liveness-watchdog.service \
  -p Type -p Result -p ExecMainStatus -p InactiveEnterTimestamp
```

### Symptom: the gateway was killed by the systemd watchdog while its own event loop was merely busy

**This is the 2026-11-03 00:12:07 UTC failure — the pre-fix signature.**

```
hermes-gateway.service: Watchdog timeout (limit 3min)!
hermes-gateway.service: Killing process <pid> (hermes) with signal SIGABRT.
Fatal Python error: Aborted
...
Current thread 0x... [hermes] (most recent call first):
  Garbage-collecting
```

**Read the faulthandler snapshot first — it names the layer that stalled.** If
the current thread is in `Garbage-collecting`, or inside a C call, the MAIN
thread was the problem. If it is parked in `selectors.select` inside
`asyncio/base_events.py`, the main thread was fine and the stall is elsewhere.

```bash
journalctl --user -u hermes-gateway.service \
  --since "<stall-start>" --until "<kill-time>" -o cat | less
# Find "Current thread" in the dump and read what it was doing.
```

**The watchdog timeout is the DETECTOR, not the cause.** systemd fired because
`WATCHDOG=1` stopped for 180 s. Before the fix that heartbeat was an `asyncio`
task on the main event loop, so a main-thread stall silenced the watchdog and
systemd killed a gateway that was about to recover.

**After the fix** this specific recurrence cannot happen from a main-thread
stall: the heartbeat comes from a thread the stall cannot stop. A main-thread
stall is now caught by the loop-liveness watchdog, which exits 75 with a full
thread dump so the cause is visible.

**How to tell the failure classes apart:**

| Evidence | Class | What to do |
|---|---|---|
| faulthandler: main thread in `Garbage-collecting` or a C call | main-thread stall | the loop-liveness watchdog owns it (dumps stacks + exits 75) |
| faulthandler: main thread parked in `selectors.select` | the loop is IDLE — something else is wrong | look at the other threads in the dump, especially `gateway-turn-*`, `session-db-*`, and `cron-parallel_*` |
| `gateway-loop-liveness-watchdog` thread present in the dump and sleeping in `stop_event.wait` | the loop watchdog had not yet reached its third strike | it was racing the systemd watchdog; this is the pre-fix behaviour |
| no `gateway-loop-liveness-watchdog` thread in the dump | `gateway.loop_watchdog: false` | the loop detector is disabled; re-enable it |

### Symptom: the gateway is alive, systemd is fed, and the loop is wedged

Check the loop-liveness watchdog's own log line. On the third consecutive
missed probe it logs at CRITICAL and exits 75:

```
Gateway event loop missed 3 consecutive liveness probes; dumping all thread stacks and exiting with code 75 so the service supervisor can restart it.
```

If that line is absent and the loop is still wedged, the watchdog was not armed:
check `gateway.loop_watchdog` in config and the thread name in the process.

### Symptom: the feeder thread is not beating

`WATCHDOG=1` can fail if the notify socket goes away or the send buffer is
full. Losing a beat is survivable — systemd tolerates a late one — but the
failure is logged:

```
Systemd watchdog heartbeat was not delivered (failure #N); systemd will time
this unit out unless a later heartbeat reaches it.
```

That line comes from `SystemdWatchdog._send_beat`. If it appears repeatedly,
the notify socket is the problem, not the feeder.

### Symptom: the feeder thread DIED

The gateway now says so itself, at CRITICAL, with the reason:

```
Systemd watchdog feeder ended unexpectedly (returned: the feeder thread returned
without being stopped); systemd will now time the unit out after 180.0s because
nothing is feeding it. The gateway stays up but is no longer supervised.
systemd watchdog feeder is unsupervised: returned: the feeder thread returned
without being stopped
```

and the runtime status is stamped `gateway_state=degraded`,
`exit_reason=systemd_watchdog_feeder_failed`.

**That status stamp is the early warning.** It appears **180 s before** systemd
kills the unit, so a degraded gateway with that exit reason is a gateway that
is about to be reclaimed. Check it:

```bash
grep -o '"gateway_state"[^,]*\|"exit_reason"[^,}]*' ~/.hermes/gateway_state.json
```

If the feeder died, the journal line above names the reason. The two endings
are distinguished:

| `failure_reason` | Meaning |
|---|---|
| `returned: the feeder thread returned without being stopped` | the feeder exited its loop for a reason nobody asked for |
| `<ExceptionType>: <message>` | the feeder raised |

A feeder that ends because the gateway asked it to (`stop()`) is **not** a
failure and is not reported — the journal must not cry wolf on every ordinary
gateway shutdown.

### Symptom: `feeder_alive` is False

`SystemdWatchdog.feeder_alive` reports whether the feeder thread is running.
It is False when the watchdog was never started, when `stop()` has run, or when
the thread died. Before this fix a feeder that died silently looked like a
frozen heartbeat to systemd, which then reclaimed the unit — the correct
outcome, but with a misleading symptom. Now the death is reported first.

### Symptom: `healthy` is False while the gateway looks fine

`healthy` is False when the feeder is not delivering. The two causes:

| Symptom | Cause |
|---|---|
| `heartbeat_failures > 0` | the datagrams are not going out (notify socket gone or full) |
| `consecutive_missed_beats > 0` | the feeder is alive but delivered nothing |
| `failed is True` | the feeder ended; see the section above |
| `unhealthy is True` | the loop-lag budget was exceeded and the feeder stopped deliberately |

`healthy` False is a **pending SIGABRT**, not a cosmetic warning.


### Note: the GIL-wedge proof unit

The GIL-wedge mechanism was verified safely with a throwaway unit, not the
production gateway: a Python main process used the real `SystemdWatchdog`,
sent `READY=1`, then held the GIL in a `ctypes.PYFUNCTYPE` call to
`libc.usleep`. With `WatchdogSec=20s`, systemd logged a watchdog timeout,
killed the wedged process, and restarted the unit with a new PID. The proof
unit was removed after the test. **Do not run a GIL-holding proof against the
production service.**

## Dependencies

- **systemd (user session)** — the unit, `WatchdogSec`, and the restart policy.
  The user session must be long-lived (`loginctl enable-linger serveradmin`).
- **`gateway/systemd_notify.py`** — sends READY and the periodic `WATCHDOG=1`.
  The periodic heartbeat comes from the **off-loop feeder thread**
  (`SystemdWatchdog._feed_forever`); `gateway/run_shutdown.py::_start_systemd_watchdog`
  starts it and sends `READY=1`, and `gateway/run.py` calls that during startup.
- **`gateway/shutdown_watchdog.py`** — the off-loop loop-liveness detector
  (`start_loop_liveness_watchdog`), which owns LOOP-wedge detection and
  escalates to a faulthandler dump plus exit 75.
- **`gateway/restart.py`** — `GATEWAY_SERVICE_RESTART_EXIT_CODE` (75), the
  exit code the loop-liveness watchdog uses.
- **`agent/memory_provider.py::spawn_context_thread`** — the feeder thread is
  started through this, not a bare `threading.Thread`, so it inherits the
  caller's contextvars. A bare thread would silently land on the default
  profile in a multiplexed gateway that serves several profiles.
- **`hermes-gateway-liveness-watchdog.timer` / `.service`** — the independent
  five-minute alerting sibling; implementation is
  `~/.hermes/scripts/gateway_heartbeat_watchdog.py`.
- **`~/.hermes/state/gateway.heartbeat`** — the loop-written heartbeat the
  alerting timer watches; it freezes with the loop.

## See Also

- `docs/wiki/services/canonical-deploy-branch.md` — the branch this PR targets.
- `docs/wiki/services/worker-memory-budget.md` — the other systemd-supervisor
  subsystem with its own independent backstop.
- Obsidian `Homelab/20-Infrastructure/Hermes/hermes-gateway.md` — the
  operator-facing page for the whole gateway (the live one; it must clearly
  distinguish the proposed PR from the deployed code).
- `gateway/AGENTS.md` — the gateway module's own conventions.
- `tests/gateway/test_systemd_notify.py` — the off-loop feeder contract.
- `tests/gateway/test_systemd_watchdog_feeder_offloop.py` — the loop-stall
  contract: the beats must keep coming while the event loop is genuinely
  stopped. This is the reproduction of the production failure.
- `tests/gateway/test_systemd_watchdog_feeder_liveness.py` — the
  failure-reporting contract: a dead or non-delivering feeder is not healthy,
  and says why.
- `tests/gateway/test_loop_wedge_safety.py` — the four bounded safety cases,
  including the disabled-guard residual risk.
- `tests/gateway/test_loop_wedge_contrast.py` — the measured BASE-vs-FIX
  contrast under a genuinely stopped event loop.
- `tests/gateway/test_loop_liveness_watchdog.py` — the loop detector's own
  contract.
