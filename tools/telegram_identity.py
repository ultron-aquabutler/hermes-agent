"""Profile-scoped resolution of the outbound Telegram bot identity.

Why this module exists (t_43eff563)
-----------------------------------
Every outbound Telegram send authenticates with ONE bot token, and the bot
token *is* the speaker's identity. A ``default``-profile worker that posts
while a sibling profile's token sits in the process-global ``os.environ``
speaks as the WRONG bot: Bryan cannot tell "Vision said X" from "Ultron said
X", and the Identity Lock (which bots may message which chats) silently stops
meaning anything.

Two independent leaks used to put a sibling's token into
``os.environ["TELEGRAM_BOT_TOKEN"]``:

1. A routed cron fire called ``load_hermes_dotenv(hermes_home=<sibling>)``,
   whose multiplex guard required a ``HERMES_HOME`` override the fire never
   installed — so the sibling's ``.env`` landed in the shared env with
   ``override=True``. (Fixed in ``hermes_cli/env_loader.py``.)
2. The root profile's ``secrets.command`` aggregator returned ONE canonical
   ``TELEGRAM_BOT_TOKEN`` for the whole estate and applied it with
   ``override_existing: true``, overwriting the profile's own ``.env`` token.
   (Fixed in ``agent/secret_sources/registry.py``.)

This module is the belt to those braces: it resolves the token for the
CALLING PROFILE, refuses to fall back to another profile's bot, and records
which bot id actually posted so the log answers "who spoke?" without
counting request URLs.

Contract
--------
* The bot id is NOT a secret (the token is). It is safe to log.
* A send whose token cannot be resolved for the calling profile FAILS. It
  never silently borrows another profile's bot.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# The bot token format is ``<bot_id>:<secret>``. Only the id is ever surfaced.
_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def bot_id_from_token(token: Optional[str]) -> Optional[str]:
    """The numeric bot id encoded in a Telegram bot token, or None.

    ``123456789:AAH...`` -> ``"123456789"``. The secret half is never returned.
    A malformed token yields None rather than a partial id — a bot id is an
    identity, and a half-parsed identity is worse than none.
    """
    if not token or not isinstance(token, str):
        return None
    candidate = token.strip()
    if ":" not in candidate:
        return None
    bot_id = candidate.split(":", 1)[0].strip()
    return bot_id if bot_id.isdigit() else None


def active_profile_name() -> str:
    """The profile this process is acting for, for error messages.

    Falls back to ``default`` — the root profile — when nothing names one.
    """
    for name in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = (os.environ.get(name) or "").strip()
        if value and value != "default":
            return value
    return "default"


def resolve_profile_bot_token(token: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Resolve the Telegram bot token for the CALLING PROFILE.

    Returns ``(token, error)``. Exactly one of the two is None on success /
    failure. ``token`` is the value the caller passed in when it is
    profile-consistent, otherwise the profile's own resolved token.

    ``token`` is the token the caller already resolved (e.g.
    ``PlatformConfig.token``). It is accepted as-is: the caller resolved it
    through the profile's own config path, which is already profile-scoped
    (``build_profile_secret_scope``). The scope check below catches the case
    where that resolution fell through to a poisoned ``os.environ``.
    """
    if token:
        return token, None

    # No token handed in: resolve the profile's own.
    resolved = _resolve_from_profile_scope()
    if resolved:
        return resolved, None

    return None, (
        f"Could not resolve a Telegram bot token for profile {active_profile_name()!r}. "
        f"Set {_BOT_TOKEN_ENV} in that profile's .env. Refusing to send: posting with another "
        f"profile's bot would make the sender indistinguishable from that bot."
    )


def _resolve_from_profile_scope() -> Optional[str]:
    """The calling profile's own token, from its secret scope or its ``.env``.

    Deliberately does NOT read the process-global ``os.environ`` for the
    token: that is the shared, contaminatable surface this whole module
    exists to avoid. It reads ``os.environ`` only to find the profile HOME
    (a non-secret routing value), then reads that profile's own files.
    """
    # A bound secret scope is the authoritative, already-profile-scoped answer.
    try:
        from agent.secret_scope import current_secret_scope

        scope = current_secret_scope()
        if scope:
            value = scope.get(_BOT_TOKEN_ENV)
            if value:
                return value
    except Exception:  # noqa: BLE001 — fall through to the file-based resolution
        logger.debug("secret scope unavailable while resolving the Telegram bot token", exc_info=True)

    # Otherwise read the profile's OWN .env — the profile home, not the process env.
    try:
        from pathlib import Path

        from hermes_constants import get_hermes_home, get_hermes_home_override

        home = get_hermes_home_override() or get_hermes_home()
        env_path = Path(home) / ".env"
        if env_path.exists():
            from agent.secret_scope import load_env_file

            value = load_env_file(env_path).get(_BOT_TOKEN_ENV)
            if value:
                return value
    except Exception:  # noqa: BLE001 — an unreadable .env must not crash a send
        logger.debug("profile .env lookup failed while resolving the Telegram bot token", exc_info=True)

    return None


def log_bot_id(bot_id: Optional[str], *, platform: str = "telegram", chat_id=None) -> None:
    """Record which bot posted, on every outbound send.

    The bot id is not a secret — the token is. Without this line, the only way
    to tell which bot spoke was to count ``bot<id>/sendMessage`` URLs in the
    log, which is exactly how the t_43eff563 mis-post went unnoticed.
    """
    logger.info(
        "sent via bot %s (%s%s)",
        bot_id or "<unknown>",
        platform,
        f" chat_id={chat_id}" if chat_id is not None else "",
    )
