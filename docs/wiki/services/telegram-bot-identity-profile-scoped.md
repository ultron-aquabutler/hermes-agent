# Telegram Bot Identity — Profile-Scoped Token Resolution (t_43eff563 / t_380891bd / t_02ebb908)

> Wiki page for the cross-profile Telegram bot-identity fix.
>
> Status: **FIXED AND COMMITTED ON A BRANCH — NOT YET MERGED INTO `canonical-deploy-live`,
> NOT YET ACTIVE IN THE RUNNING GATEWAY.**
>
> The fix lives on branch `fix/telegram-bot-identity-phase3-t_380891bd-live`
> (worktree `.worktrees/t_380891bd_p3`). The live branch `canonical-deploy-live`
> (HEAD `48b1a1e3ce3659aa3193d98b64d3e6329741085b`) — the branch the running gateway
> (PID 3098267) executes — still carries **both** defects, verified by direct execution
> against the live tree (see "Reproduction" below).
>
> The original fix was committed as `55184077da`
> (`fix(telegram): scope the outbound bot identity to the calling profile (t_380891bd)`)
> on branch `fix/telegram-bot-identity-phase3-t_380891bd`. That commit's five source
> files are **byte-identical** to the port now on the live-based branch. It was not
> merged: the live branch had moved **17,361 commits** past the fix's base, so the
> port had to be re-applied onto current live HEAD.
>
> Regression on the landed tree: `tests/tools/test_telegram_bot_identity_profile_scoped.py`
> + `tests/gateway/test_telegram_outbound_bot_id_logged.py` -> **24 passed**.

## Overview

A **default-profile** worker posted to Telegram using **Ultron's bot token**, so a
message that should have read as "Vision said X" reached Bryan as "Ultron said X".

That breaks the **Identity Lock** — the rule that each bot speaks only as itself. Once
two bots can post to the same chat under the same identity, every message in that chat
is ambiguous, and no later audit can tell the two apart.

Two **independent** credential leaks put a sibling profile's token where the send path
could see it. Both were necessary for the observed mis-post, and both had to be closed.

### Leak A — a routed cron fire poisons the shared process environment

A routed cron fire calls `load_hermes_dotenv(hermes_home=<sibling>)`. The multiplex
guard in `hermes_cli/env_loader.py` decided whether that load may write into the
**process-global** `os.environ`:

```python
# hermes_cli/env_loader.py:685  (LIVE branch, still present)
if (is_multiplex_active() and get_hermes_home_override() is not None
        and (home_path.resolve() != launch_home or get_hermes_home().resolve() != launch_home)):
```

The guard requires `get_hermes_home_override() is not None`. A routed fire never
installs a home override — `cron/scheduler.py::_install_fire_secret_scope` calls
`set_multiplex_context(True)` and installs a **secret scope**, not a `HERMES_HOME`
override. So the clause was false, the guard did not fire, and the routed sibling's
`.env` was written into the shared `os.environ` with `override=True`.

Every later resolution that reads `os.environ` (any consumer with no secret scope
bound — including `gateway/config.py::_getenv`) then answered with the **routed**
profile's credential. A default-profile worker in that same process built its Telegram
adapter with Ultron's token.

> Live's own comment at `env_loader.py:679-680` already names the routed-cron-fire
> case — and the guard still required the one thing a routed fire does not set.

### Leak B — the cluster `secrets.command` bulk source overwrites the profile's own token

The root profile's `config.yaml` enables a cluster-level secret source:

```yaml
secrets:
  command:
    enabled: true
    override_existing: true
```

A **bulk** source (`secrets.command`) returns **one** value per process. That value
belongs to exactly one profile. With `override_existing: true` it overwrote the
launch profile's own `.env` token, so the *ambient* `os.environ` held a sibling's bot
token even with no routed fire involved.

`agent/secret_scope.build_profile_secret_scope` already applied the right rule
(a profile's own `.env` wins) — but only for the **per-profile scope** path. The
**process-global** path did not, so the two paths disagreed about the same credential.

### The consumer link

```
gateway/config.py::load_gateway_config()
  -> _apply_env_overrides(config)
     -> gateway/config_env.py
        -> gateway/config.py::_getenv(name)
           -> os.environ.get(name)          # when no secret scope is bound
  -> config.platforms[Platform.TELEGRAM].token
     -> plugins/platforms/telegram/adapter.py -> Bot(token) -> api.telegram.org/bot<id>/sendMessage
```

`os.environ` is shared by every profile in the process. That single fact is what turns
both leaks into a mis-post.

## Architecture

### Before (live, HEAD `48b1a1e3ce`)

```
  routed cron fire (sibling)
        |
        v
  load_hermes_dotenv(hermes_home=<sibling>)
        |  guard needs get_hermes_home_override() -> None  ==>  DOES NOT FIRE
        v
  sibling's .env  --override=True-->  os.environ   [LEAK A]
        |
        |  cluster secrets.command (bulk, one value per process)
        |  override_existing: true
        v
  os.environ[TELEGRAM_BOT_TOKEN] = a sibling's token   [LEAK B]
        |
        v
  gateway/config.py::_getenv  ->  os.environ.get("TELEGRAM_BOT_TOKEN")
        |
        v
  TelegramAdapter -> Bot(token) -> posts as the WRONG bot
```

### After

```
  routed cron fire (sibling)
        |
        v
  load_hermes_dotenv(hermes_home=<sibling>)
        |  guard keys on the ROUTED TARGET home, not on an override a fire never sets
        v
  SKIPPED - the sibling's .env never reaches os.environ        [LEAK A CLOSED]

  cluster secrets.command (bulk)
        |
        |  a bulk source may not override a name the profile's own .env defines
        v
  "kept the profile's own .env value" - skipped_existing         [LEAK B CLOSED]

  gateway/config.py::_reassert_profile_scoped_tokens(config, home)
        |
        |  the profile's own .env is authoritative for the platform-identity credential
        v
  config.platforms[TELEGRAM].token = THIS profile's token

  tools/telegram_identity.py::resolve_profile_bot_token(token)
        |
        |  refuse to borrow another profile's bot; log which bot posted
        v
  send proceeds only under the CALLING profile's own bot
```

### Files

| File | Role |
|---|---|
| `hermes_cli/env_loader.py` | The multiplex guard: keys on the routed **target** home instead of a home override a routed fire never sets. Closes Leak A. |
| `agent/secret_sources/registry.py` | A **bulk** source may not override a name the profile's own `.env` defines. Placeholder values (`__SOURCE_MANAGED__`) stay source-managed. Closes Leak B. |
| `gateway/config.py` | `_reassert_profile_scoped_tokens()` — re-asserts the profile's own `.env` token for the platform-identity credentials after the ambient env overrides. |
| `tools/telegram_identity.py` | **New.** Resolves the bot token for the **calling** profile; refuses to borrow another profile's bot; records which bot posted. |
| `plugins/platforms/telegram/standalone.py` | The one-shot send path: resolve the token before anything is sent; log the bot id on every send. |
| `plugins/platforms/telegram/adapter.py` | The **gateway** send path (cron delivery, gateway replies, media, drafts) — logs the bot id on every outbound send. |
| `tools/audit_telegram_bot_identity.py` | **New.** Audits retained logs for sends made under the wrong bot. |
| `tests/tools/test_telegram_bot_identity_profile_scoped.py` | The credential-scoping regression tests. |
| `tests/gateway/test_telegram_outbound_bot_id_logged.py` | The bot-id-logging regression tests. |

> **Note on `tools/send_message_senders.py`:** the original PHASE 3 commit
> (`55184077da`) patched the Telegram send inside `tools/send_message_senders.py`.
> Live has since moved the Telegram send into the plugin
> (`plugins/platforms/telegram/standalone.py`), so that file no longer contains a
> Telegram send and the port adapts the same change to the plugin. The port's
> `tools/send_message_senders.py` is therefore **identical to live** — deliberately.

## Operation

Nothing to run. The fix is a library change; it takes effect when the tree that
contains it is the tree the gateway executes.

### Verifying the fix is live

```bash
cd /home/serveradmin/.hermes/hermes-agent
git rev-parse canonical-deploy-live                     # expect the live HEAD

# Leak A closed: the guard keys on the routed TARGET home. Match the CODE, not the
# comment that names the old clause:
grep -n "routed_target = home_path.resolve() != launch_home" hermes_cli/env_loader.py
                                                       # expect a match

# Leak B closed: a bulk source may not override the profile's own .env credential
grep -n "bulk_cannot_override" agent/secret_sources/registry.py   # expect a match

# The profile's own token wins for the platform-identity credential
grep -n "_reassert_profile_scoped_tokens" gateway/config.py      # expect a match

# The bot id is logged on every outbound send
grep -n "sent via bot" tools/telegram_identity.py                # expect a match
```

A gateway **restart** is required for the change to affect running sessions. A restart
tears down the unit cgroup and kills in-flight workers, so it is a **human-gated** step
requiring Bryan's explicit permission — it is not part of this change.

## Configuration

No configuration is added or changed by this fix. It makes the **existing**
configuration behave as documented:

- The profile's own `.env` is authoritative for any key it defines (the rule
  `agent.secret_scope.build_profile_secret_scope` already applied to the per-profile
  scope; the process-global path now applies it too).
- A bulk `secrets.command` source returns one value per process, so it may not own a
  per-profile credential.

The bot id is **not** a secret (the token is) — it is logged so that "which bot spoke?"
is answerable from a log line.

## Troubleshooting

### "Which bot posted this?"

```bash
grep -h "sent via bot" ~/.hermes/logs/gateway.log ~/.hermes/profiles/*/logs/*.log
```

Before this fix the only way to answer was to count `bot<id>/sendMessage` URLs in the
httpx request lines — which is exactly how the original mis-post went unnoticed for a
day. `tools/audit_telegram_bot_identity.py` automates that reading and is explicit
about what the retained logs **cannot** prove.

### The audit reports "0 outbound calls seen" — is that good news?

**No — it means the retained logs do not record outbound bot ids at all.** The httpx
request line (`POST https://api.telegram.org/bot<id>:<token>/sendMessage`) is only
present in a log whose logging captures it. "No bot id in this log" means "this log
does not record outbound bot ids", **not** "no mis-post happened". The fix exists
because that blind spot is what made the original incident invisible.

### A send was refused with "Telegram send refused: ..."

`tools/telegram_identity.resolve_profile_bot_token()` refuses a send that would go out
under another profile's bot. The refusal means the calling profile could not resolve
**its own** token. Check that the profile's own `.env` defines `TELEGRAM_BOT_TOKEN`
(or that its per-profile secret scope resolves it) — do **not** work around it by
copying a sibling's token in.

### "kept the profile's own .env value" in the source log

That is the Leak B fix working, not an error. The bulk source offered a value for a
credential the profile's own `.env` already defines; the profile's value won. The
message names the credential, never its value.

## Dependencies

- `agent.secret_scope` — `load_env_file`, `is_multiplex_active`, `build_profile_secret_scope`.
- `hermes_constants` — `get_hermes_home`, `get_hermes_home_override`, `get_process_hermes_home`.
- `cron.scheduler` — `_install_fire_secret_scope` (the routed-fire producer of Leak A).
- `gateway.config_env` — `_apply_env_overrides` (the ambient-env consumer).

## See Also

- `docs/wiki/services/duplicate-tool-call-ids.md` — the other live-branch port that
  needed the same "the fix is committed but not merged" treatment.
- Cards: `t_43eff563` (the parent fix), `t_380891bd` (PHASE 3 — the bot-id logging),
  `t_45fa4871` (the earlier, narrower attempt), `t_02ebb908` (this port's card).
