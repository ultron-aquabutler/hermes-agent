# Telegram Bot Identity — Profile Scoping

A Telegram bot token **is an identity**. When one profile's send path resolves another
profile's token, the first profile's messages arrive signed as the second — every message in
the chat becomes ambiguous about who spoke. That is what happened on 2026-11-11: a
default-profile worker posted to Bryan's DM using the `ultron` bot's token.

Written 2026-11-17 by @friday, alongside the fix (kanban `t_02ebb908`, parent `t_d7ea1581`).

## Overview

Two independent leaks put a sibling profile's token where the send path could see it. Both had
to be closed — fixing either one alone leaves the mis-post possible.

| | Leak | Mechanism |
|---|---|---|
| **A** | Routed cron fire poisons the shared process env | `cron/scheduler.py::_install_fire_secret_scope` turns multiplex semantics ON for a routed fire (`set_multiplex_context(True)`) but installs a *secret scope*, not a `HERMES_HOME` override. The multiplex guard in `hermes_cli/env_loader.py` required `get_hermes_home_override() is not None` — the one thing a routed fire never sets — so the guard did not fire and the routed profile's `.env` was written into the process-global `os.environ` with `override=True`. |
| **B** | Cluster `secrets.command` overwrites the profile's own token | A bulk secret source returns ONE canonical value per process, so that value belongs to exactly one profile. With `override_existing: true` it overwrote the launch profile's own `.env` token. |

Either one alone is enough to make a profile post as the wrong bot.

## Architecture

Three layers, each closing a different part of the same failure.

**1. Load-time guard — `hermes_cli/env_loader.py`**

The multiplex guard now keys on the *routed target home*, not on a home override the routed
fire never installs:

```python
routed_target = home_path.resolve() != launch_home
active_foreign = get_hermes_home().resolve() != launch_home
if is_multiplex_active() and (routed_target or active_foreign):
    return  # a ROUTED profile's .env must not enter the shared os.environ
```

The launch home's own `.env` still loads under multiplex semantics — it is process
configuration, not a routed profile's credential.

**2. Source-time guard — `agent/secret_sources/registry.py`**

A bulk source may not override a name the home's own `.env` defines:

```python
if bulk_cannot_override and var in self.profile_owned and not self._claimed_by_other_source(var):
    sr.skipped_existing.append(var)
```

`_profile_owned_names()` excludes PLACEHOLDER values (`__<SOURCE>_MANAGED__`) — a placeholder
means "source-managed", and the source stays authoritative for it (the #74265 flow).

**3. Config-time re-assertion — `gateway/config.py`**

`_apply_env_overrides` reads tokens through `_getenv`, which falls back to the shared
`os.environ` when no secret scope is bound. `_reassert_profile_scoped_tokens` re-resolves each
platform token from the profile's own `.env`, for the credential names that identify a
*platform identity* (`PLATFORM_TOKEN_ENV_NAMES`). A bot token is an identity; re-asserting it
is what makes the profile's own `.env` authoritative on the process-global path too.

**Observability — `plugins/platforms/telegram/adapter.py`**

Every outbound send logs the numeric **bot id** it posted as:

```
[telegram] telegram outbound message bot_id=8851168207 chat=-1004452024623
```

The bot id is NOT a secret (the token is). Before this, the only way to tell which bot spoke
was to count `bot<id>/sendMessage` URLs in the log — which is exactly how the mis-post went
unnoticed for a day.

## Operation

**Audit every profile's identity:**

```bash
cd ~/.hermes/hermes-agent
venv/bin/python tools/audit_telegram_bot_identity.py --home ~/.hermes
```

```
Telegram bot identity audit — home: /home/serveradmin/.hermes
  default      own_bot_id=8851168207     ambient_bot_id=(none)
  curator      own_bot_id=(none)         ambient_bot_id=(none)
  friday       own_bot_id=8979456313     ambient_bot_id=(none)
  hermes-gateway own_bot_id=(none)       ambient_bot_id=(none)
  librarian    own_bot_id=(none)         ambient_bot_id=(none)
  shuri        own_bot_id=8912593428     ambient_bot_id=(none)
  ultron       own_bot_id=8810592884     ambient_bot_id=(none)
```

Exit code `1` means at least one profile's own identity and the ambient identity disagree.

**Watch the outbound log:**

```bash
grep 'telegram outbound' ~/.hermes/logs/gateway.log
```

**Resolve a profile's own token in code:**

```python
from tools.telegram_identity import resolve_bot_token, bot_id_from_token, check_identity

token = resolve_bot_token("ultron")
bot_id = bot_id_from_token(token)
check_identity("ultron", token)   # raises IdentityMismatch if the token is a sibling's
```

## Configuration

| Setting | Where | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | each profile's own `.env` | Authoritative for that profile. `$HERMES_HOME/.env` for `default`; `$HERMES_HOME/profiles/<name>/.env` for the rest. |
| `secrets.command` | cluster-level | A **bulk** source. May supply names the profile does not define; may NOT override a name the profile's own `.env` defines. |
| `PLATFORM_TOKEN_ENV_NAMES` | `gateway/config.py` | The credential names re-asserted as platform identity. |

Bot ids as of 2026-11-17 (read from the live profile `.env` files):

| Profile | Bot id |
|---|---|
| `default` | `8851168207` |
| `friday` | `8979456313` |
| `ultron` | `8810592884` |
| `shuri` | `8912593428` |

## Troubleshooting

**A message arrived from the wrong bot.**
Check the outbound log for the `bot_id=` on the send that produced it. If the bot id is not
the profile's own, run the audit tool — a non-zero exit names the offending profile.

**The audit reports a MISMATCH.**
The ambient environment holds a different bot's token than the profile's own `.env`. Run the
audit with `--json` to see both ids. Usual causes: a routed cron fire that leaked into the
shared env (Leak A), or a bulk `secrets.command` still overriding the profile's token
(Leak B).

**`IdentityMismatch` raised.**
Working as intended — the resolved token belongs to a different profile. Posting as another
bot is worse than not posting. Check which `.env` the running profile actually loaded.

**`bot_id=unknown` in the outbound log.**
The adapter's token is not a standard `<bot_id>:<secret>` shape — either a self-hosted Bot API
server, or no token configured. The log line still records the chat, so the send is
attributable to a profile; only the bot id is unknown.

## Dependencies

- `agent.secret_scope.load_env_file` — the profile's own `.env` reader
- `agent.secret_sources.registry` — the secret-source applier
- `gateway.config` — the gateway's platform configuration loader
- `python-telegram-bot` — the Telegram client (token → bot id shape)

## See Also

- `docs/wiki/services/deploy-branch-guard.md`
- `docs/wiki/services/managed-worker-cgroup-isolation.md`
- Obsidian: `Homelab/10-Services/Services/telegram-bot-identity-profile-scoping.md`
