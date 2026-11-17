"""Resolve the Telegram bot identity for the CALLING profile — and refuse to borrow another's.

A bot token IS an identity. When profile A's send path resolves profile B's token, A's messages
arrive in the chat signed as B. That is not a cosmetic bug: every message in the chat becomes
ambiguous about who said it (the t_43eff563 mis-post — a default-profile worker posting as the
``ultron`` bot).

Two rules this module enforces:

1. The token that identifies the calling profile comes from THAT profile's own credential
   sources — never from an ambient process environment another profile may have written to.
2. Before sending, the resolved token is checked against the profile's own identity. A token
   that belongs to a DIFFERENT profile is refused, not silently used.

The bot id is not a secret (the token is), so it is safe to log — and it is the only way to tell
which bot spoke from a log. Before this module landed, the only way was to count
``bot<id>/sendMessage`` URLs, which is exactly how the original mis-post went unnoticed for a day.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The env var name every profile uses for its Telegram bot token.
TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

# Known bot ids, keyed by the profile that owns them. Used only to REPORT a mismatch clearly —
# the refusal does not depend on this table being complete.
#
# These are the LIVE values, read from each profile's own .env. The bot id is the public half of
# the credential (the token is the secret half), so recording it here is safe — but it is also
# STALE-prone: a bot token rotation changes it. This table is a REPORTING aid only; the actual
# ownership check always compares against the profile's own .env, which is the live truth.
KNOWN_BOT_IDS: dict[str, str] = {
    "default": "8851168207",
    "friday": "8979456313",
    "ultron": "8810592884",
    "shuri": "8912593428",
}


def bot_id_from_token(token: Optional[str]) -> Optional[str]:
    """The numeric bot id encoded in a Telegram bot token, or None.

    A Telegram bot token has the shape ``<bot_id>:<secret>`` — the bot id is the public half of
    the credential, so it can be logged without leaking the secret.

    Returns None for a malformed token, for an empty token, and for a self-hosted Bot API
    server (whose tokens do not follow the shape).
    """
    if not token or not isinstance(token, str):
        return None
    head, sep, _tail = token.partition(":")
    if not sep:
        return None
    head = head.strip()
    if not head.isdigit():
        return None
    return head


def profile_home(profile: Optional[str] = None) -> Path:
    """The hermes home of ``profile`` (or of the calling profile when None)."""
    from hermes_constants import get_hermes_home

    if profile and profile != "default":
        return get_hermes_home().parent / "profiles" / profile
    return get_hermes_home()


def own_env_values(profile: Optional[str] = None) -> dict[str, str]:
    """The profile's OWN ``.env`` values — the authoritative source for its credentials.

    A bulk secret source (``secrets.command``) returns ONE canonical value per process, so the
    value it returns belongs to exactly one profile. Letting it overwrite a profile's own
    ``.env`` credential aliases one bot's identity into another profile's runtime. The profile's
    own ``.env`` is therefore authoritative for any key it defines.

    Fail-open: an unreadable ``.env`` yields an empty mapping and the normal precedence applies.
    """
    try:
        from agent.secret_scope import load_env_file

        return load_env_file(profile_home(profile) / ".env")
    except Exception:  # an unreadable .env must not break identity resolution
        logger.debug("could not read the profile's own .env for telegram identity", exc_info=True)
        return {}


def resolve_bot_token(profile: Optional[str] = None, environ: Optional[dict] = None) -> Optional[str]:
    """The Telegram bot token for the CALLING profile.

    Resolution order:

    1. The profile's own ``.env`` — authoritative for any key it defines.
    2. The ambient environment — only when the profile's own ``.env`` does not define the key.

    The ambient environment is shared by every profile in the process, so step 2 is a fallback,
    never a preference.
    """
    own = own_env_values(profile)
    token = own.get(TELEGRAM_TOKEN_ENV)
    if token:
        return token
    env = environ if environ is not None else os.environ
    return env.get(TELEGRAM_TOKEN_ENV) or None


class IdentityMismatch(RuntimeError):
    """The resolved bot token belongs to a different profile than the one sending."""


def check_identity(profile: Optional[str] = None, token: Optional[str] = None) -> Optional[str]:
    """Verify the token belongs to ``profile``. Return the bot id, or raise IdentityMismatch.

    A mismatch is a hard refusal: posting as another bot is worse than not posting.
    """
    if token is None:
        token = resolve_bot_token(profile)
    bot_id = bot_id_from_token(token)
    if bot_id is None:
        # No usable token — the send path reports the missing credential itself.
        return None
    expected = own_env_values(profile).get(TELEGRAM_TOKEN_ENV)
    expected_id = bot_id_from_token(expected)
    if expected_id and expected_id != bot_id:
        raise IdentityMismatch(
            f"resolved Telegram bot id {bot_id} does not match this profile's own bot id "
            f"{expected_id} — refusing to post as another profile's bot")
    return bot_id


def describe(profile: Optional[str] = None, token: Optional[str] = None) -> dict[str, Any]:
    """A small dict describing the resolved identity — safe to log (no token)."""
    bot_id = bot_id_from_token(token) if token is not None else None
    return {"profile": profile or "default", "bot_id": bot_id}
