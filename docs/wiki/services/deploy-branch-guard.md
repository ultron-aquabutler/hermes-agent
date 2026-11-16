# Deploy Branch Guard

The deploy branch guard is the thing that stops a running Hermes install from quietly
drifting off its canonical branch. It answers one question on every dispatch tick:
*is the deployed tree on the branch we said it was?* — and under `strict` it refuses the
tick when the answer is "no". This page is the runbook for that subsystem: how it
resolves the tree it inspects, how the three guard modes differ, why `missing_repo` is
**not** a drift event, and what to do when a dispatch tick dies at the guard.

Written 2026-11-16 by @friday, alongside the fix for the strict-mode refusal
(kanban `t_59318511`), on top of the original guard (`t_3d00480e` / commit `e55b7eaa23`).

## Overview

The guard lives in `hermes_cli/deploy_guard.py` and is called from
`hermes_cli/kanban_db_dispatch.py` inside `dispatch_once` — **outside** the
`_dispatch_tick_lock` (taken later, at `:2277`). A raise propagates out of
`dispatch_once`, the CLI returns `rc=1`, and that board stops dispatching. There is no
lock wedge, but the board is dead until the drift or the config is fixed.

What it checks:

| Outcome | `reason` | Drift? | Strict raises? |
|---|---|---|---|
| Tree is on the expected branch | `match` | no | no |
| Tree is on a different branch | `wrong_branch` | **yes** | **yes** |
| HEAD is detached | `detached` | **yes** | **yes** |
| Resolved path has no `.git` | `missing_repo` | no | no (since `t_59318511`) |
| `git` binary not available | `git_unavailable` | no | no (since `t_59318511`) |

The last two are **indeterminate**, not drifted: the guard could not *read* the tree, so
it has no evidence the tree is wrong. Treating them as drift is what broke
profile-scoped dispatch (see Troubleshooting).

## Architecture

```
~/.hermes/                                   # HERMES_HOME (root install)
├── hermes-agent/                            # the DEPLOYED tree the guard inspects
│   └── .git/                                # a real repo — branch + HEAD readable
└── profiles/
    ├── curator/                             # a PROFILE home — no hermes-agent/ child
    ├── librarian/
    └── friday/
```

Resolution order for the tree under inspection (`_agent_home()`):

1. `HERMES_AGENT_HOME` — absolute, existing directory → **use it**.
2. `HERMES_AGENT` — absolute, existing directory → **use it**.
3. `<HERMES_HOME>/hermes-agent` — the candidate, **returned even if it does not exist**.
4. `DEFAULT_AGENT_HOME` — the compile-time default.

Rung 3 is deliberately *not* "fall through if missing". Returning the non-existent
candidate makes the guard report `missing_repo` (non-fatal) instead of silently
inspecting the **real live tree** — that fall-through is what made an isolated test
reach into `~/.hermes` and trip the `home_io_guard` fixture (`t_3d00480e`).

### Where the guard runs, and why `HERMES_HOME` alone is not enough

A **profile-scoped** process runs with `HERMES_HOME=~/.hermes/profiles/<name>`, which has
no `hermes-agent/` child. Rung 3 therefore resolves to a path that is not a repo, and the
guard reports `missing_repo`. Two things follow:

- The **check** must not treat that as drift (the `t_59318511` fix in
  `hermes_cli/deploy_guard.py`).
- The **deployment sites** must pin `HERMES_AGENT_HOME` so the check inspects the real
  deployed tree and is genuinely *enforced* rather than skipped.

## Operation

### Modes

| Mode | Behaviour |
|---|---|
| `off` | No-op. Returns a synthetic `ok=True, reason="match"`. |
| `warn` (default) | Logs at WARNING, prints a stderr banner, returns the real result. Never raises. |
| `strict` | As `warn`, plus raises `DeployBranchDriftError` on **drift** (`wrong_branch`, `detached`). |

`missing_repo` and `git_unavailable` are logged at **ERROR** in every mode — louder
than a drift warning — so a misresolved `agent_home` stays visible even though it no
longer refuses the tick. The ERROR line names the resolved `agent_home`, which is the
first thing to check when you see it.

### Reading the guard's state by hand

```bash
# What does the guard resolve for this process, and is it happy?
# (HERMES_AGENT_HOME is what a deployed process pins; without it a
#  profile-scoped HERMES_HOME resolves a path that is not a repo.
#  HERMES_DEPLOY_BRANCH matters too: the code default is
#  "canonical-deploy", while the live deployed branch is
#  "canonical-deploy-live" — so a process WITHOUT the pin compares
#  against the wrong name and reports wrong_branch.)
cd /home/serveradmin/.hermes/hermes-agent
HERMES_AGENT_HOME=/home/serveradmin/.hermes/hermes-agent \
HERMES_DEPLOY_BRANCH=canonical-deploy-live \
./venv/bin/python -c "
from hermes_cli.deploy_guard import check_deploy_state
r = check_deploy_state()
print(r.reason, r.agent_home, r.observed_branch, r.observed_sha)
"
```

Expected on a healthy deployed tree:

```
match /home/serveradmin/.hermes/hermes-agent canonical-deploy-live <sha>
```

> **The code default and the live branch differ.** `DEFAULT_DEPLOY_BRANCH` is
> `canonical-deploy`; the deployed branch is `canonical-deploy-live`. A process that
> lacks `HERMES_DEPLOY_BRANCH` therefore compares against the wrong name and reports
> `wrong_branch` — a false positive. The systemd drop-in pins the right value, so the
> gateway is fine, but ad-hoc shells and any dispatch path that does not inherit the
> unit's env are not.

### Logs

The banner goes to the structured log (`hermes_cli.deploy_guard`) **and** to stderr,
which the cron pump captures. On this host:

| What | Where |
|---|---|
| Root-home gateway | `~/.hermes/logs/agent.log` |
| Profile-scoped gateway | `~/.hermes/profiles/<name>/logs/agent.log` |

A healthy root-home line looks like:

```
[deploy-guard] OK: HEAD=964fe1612653 on branch canonical-deploy-live
```

A misresolved profile home looks like:

```
[deploy-guard] WARN: /home/serveradmin/.hermes/profiles/curator/hermes-agent has no .git — deploy-branch check skipped.
```

That WARN is the signal to pin `HERMES_AGENT_HOME` at the deployment site.

### Deployment-site pinning

`HERMES_AGENT_HOME` must be pinned wherever a profile-scoped process runs a dispatch:

| Site | How |
|---|---|
| systemd gateway units | `Environment="HERMES_AGENT_HOME=/home/serveradmin/.hermes/hermes-agent"` in `~/.config/systemd/user/hermes-gateway.service.d/02-deploy-guard.conf` |
| `curator_dispatch_cron.py` | `env["HERMES_AGENT_HOME"] = AGENT_HOME` in `_dispatch_env()` |
| `librarian_dispatch_cron.py` | same |

The cron scripts keep the caller's `HERMES_HOME` (so the subprocess resolves the same
board root and credentials) and pin `HERMES_AGENT_HOME` **on top of it** — the guard's
own resolution order makes the override win.

> **Changing the systemd drop-in does not restart anything.** systemd reads the file on
> the next `daemon-reload` + unit restart. A gateway restart needs Bryan's explicit
> per-message authorization — do not restart it as part of a guard fix.

## Configuration

| Setting | Where | Notes |
|---|---|---|
| `HERMES_DEPLOY_GUARD` | env | `off` / `warn` / `strict`. Default `warn`. |
| `HERMES_DEPLOY_BRANCH` | env | The expected branch. Default `canonical-deploy-live`. |
| `HERMES_AGENT_HOME` | env | The tree the guard inspects. Overrides `HERMES_HOME` derivation. |
| `HERMES_AGENT` | env | Legacy alias for the above. |
| `HERMES_HOME` | env | Selects the data root; also derives the inspected tree when nothing else is pinned. |

The live gateway configuration lives in
`~/.config/systemd/user/hermes-gateway.service.d/02-deploy-guard.conf`.

## Troubleshooting

### A dispatch tick dies with `DeployBranchDriftError` and rc=1

**Symptom.** `hermes kanban --board <board> dispatch` returns 1; the board stops
dispatching; the log shows a `DRIFT:` banner.

**Cause.** The deployed tree is genuinely on the wrong branch (or detached).

**Fix.** Decide which is true:

```bash
# What branch is the deployed tree actually on?
git -C /home/serveradmin/.hermes/hermes-agent rev-parse --abbrev-ref HEAD
git -C /home/serveradmin/.hermes/hermes-agent status --short --branch
```

- The tree is on a **different branch** → the fix was committed somewhere else. Move the
  commit onto `canonical-deploy-live` (fast-forward), or set `HERMES_DEPLOY_BRANCH` to the
  branch that is genuinely canonical.
- The tree is **detached** → someone checked out a specific SHA. Re-attach:
  `git -C /home/serveradmin/.hermes/hermes-agent checkout canonical-deploy-live`.

### `missing_repo` under strict — the profile-scoped dispatch refusal (fixed in `t_59318511`)

**Symptom (pre-fix).** `hermes kanban --board curator dispatch` returned **rc=1** with a
`DeployBranchDriftError` traceback naming `missing_repo`, and the curator/librarian
boards stopped dispatching. Live corroboration was in
`~/.hermes/profiles/<name>/logs/agent.log`:

```
[deploy-guard] WARN: /home/serveradmin/.hermes/profiles/curator/hermes-agent has no .git
```

**Cause.** Two halves of one bug:

1. `check_deploy_state()` classifies `missing_repo` as a **non-fatal, non-drift** reason,
   but `check_deploy_branch()`'s strict path raised on *any* reason other than `match`.
   The module's own contract and its strict path disagreed — and the disagreement
   refused dispatch.
2. The `HERMES_HOME` rung of `_agent_home()` was added for **test isolation** (an
   isolated test points `HERMES_HOME` at a tmpdir; falling back to `DEFAULT_AGENT_HOME`
   would read the REAL live tree and trip `tests/home_io_guard.py`). The same rung also
   applies in production, where `HERMES_HOME` is a *profile* home with no
   `hermes-agent/` child — so it misresolved there too.

**Fix (both halves).**

1. `check_deploy_branch()` raises on **drift only** — `wrong_branch` / `detached`.
   `missing_repo` / `git_unavailable` are logged at ERROR with the resolved
   `agent_home` and do not refuse the tick.
2. The deployment sites pin `HERMES_AGENT_HOME` (see Operation) so the check inspects
   the **real deployed tree** — the guard is genuinely enforced, not skipped.

**What was deliberately NOT done.** Making `_agent_home()` fall through a profile home
with no `.git` to the deployed tree. That re-breaks test isolation: an isolated test
whose `HERMES_HOME` is a tmpdir would fall through to the real live tree and trip
`tests/home_io_guard.py` — re-introducing the `t_3d00480e` failure.

### The guard reports `missing_repo` but dispatch works

That is the intended post-fix behaviour: the check could not read the tree, so it did
not refuse the tick, but it logged at ERROR because the resolved home is probably wrong.
Pin `HERMES_AGENT_HOME` at the deployment site so the check inspects the real tree.

### `git_unavailable`

The `git` binary is not on `PATH` for that process. It is logged at ERROR and does not
refuse the tick. Common in slim container images; install `git` or point `PATH` at it.

## Dependencies & Topology

**Depends on:**

- The deployed tree `~/.hermes/hermes-agent` — a real git checkout, readable by the
  guard's `git` subprocess.
- `git` on `PATH` for the inspecting process.
- The systemd unit drop-in
  `~/.config/systemd/user/hermes-gateway.service.d/02-deploy-guard.conf` — where the
  guard's env is pinned.

**Depended on by:**

- `hermes_cli/kanban_db_dispatch.py` — every board's dispatch tick.
- The gateway, the cron pump, and the profile-scoped dispatch cron scripts.

**Topology:** entirely local. No network service, no port. The guard shells out to
`git` in the tree it inspects and compares the branch name against
`HERMES_DEPLOY_BRANCH`.

## See Also

- `hermes_cli/deploy_guard.py` in `ultron-aquabutler/hermes-agent` — the implementation.
- `tests/hermes_cli/test_deploy_guard.py` — the behaviour contract, including the
  strict-mode regression tests added by `t_59318511`.
- `tests/home_io_guard.py` — the test-isolation fixture that constrains
  `_agent_home()`'s resolution.
- [[Services/hermes-pm-dependency-environment]] — the other half of agent boot: which
  dependency generation a worker resolves.
- [[Homelab/50-Agent-Memory/Friday/index|Friday — Memory Index]] — Friday's durable lessons.
- Kanban `t_59318511` — the fix card for the strict-mode refusal;
  `t_3d00480e` — the card that introduced strict mode.
