"""Regression: a profile must never post to Telegram as a SIBLING profile's bot.

The t_43eff563 mis-post: a default-profile worker posted to Telegram using ULTRON's bot
token. Two INDEPENDENT leaks put a sibling's token where the send path could see it; both had
to be closed for the observed mis-post.

Leak A — a routed cron fire poisons the shared process environment.
  ``cron/scheduler.py::_install_fire_secret_scope`` turns multiplex semantics ON for the
  span of a routed fire (``set_multiplex_context(True)``) but installs a SECRET SCOPE, not a
  HERMES_HOME override. The multiplex guard in ``hermes_cli/env_loader.py`` required
  ``get_hermes_home_override() is not None`` — the one thing a routed fire never sets — so
  the guard did not fire and the ROUTED sibling's ``.env`` was written into the process-global
  ``os.environ`` with ``override=True``.

Leak B — the cluster ``secrets.command`` bulk source overwrites the profile's own token.
  A bulk source returns ONE canonical value per process, so that value belongs to exactly one
  profile. With ``override_existing: true`` it overwrote the launch profile's own ``.env``
  token, so the ambient env held a sibling's bot token with no routed fire involved.

These tests exercise the leaks by DIRECT EXECUTION against a synthetic home, not by reading
the source.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    yield


# --------------------------------------------------------------------------------------
# Leak A — a routed cron fire poisons the shared process environment
# --------------------------------------------------------------------------------------


def test_routed_cron_fire_does_not_poison_process_env(monkeypatch, tmp_path):
    """A routed fire's sibling ``.env`` must NOT land in the shared ``os.environ``.

    This is the exact shape of the routed cron fire: multiplex semantics ON, no HERMES_HOME
    override, a load targeted at a sibling profile's home.
    """
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from hermes_cli.env_loader import load_hermes_dotenv

    launch_home = tmp_path / "launch"
    routed_home = tmp_path / "profiles" / "ultron"
    launch_home.mkdir(parents=True)
    routed_home.mkdir(parents=True)

    # The process's own home is the launch home — the sibling home is a ROUTE target.
    monkeypatch.setenv("HERMES_HOME", str(launch_home))

    (routed_home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=8979456313:AAA-sibling-bot-secret\n", encoding="utf-8"
    )

    # The routed fire: multiplex semantics ON, NO home override installed.
    token = set_multiplex_context(True)
    try:
        load_hermes_dotenv(hermes_home=routed_home)
    finally:
        reset_multiplex_context(token)

    leaked = os.environ.get("TELEGRAM_BOT_TOKEN")
    assert leaked is None, (
        "the routed sibling's .env leaked into the shared process environment "
        f"(TELEGRAM_BOT_TOKEN={leaked!r}) — the default profile will post as that bot"
    )


def test_launch_home_dotenv_still_loads_under_multiplex(monkeypatch, tmp_path):
    """The fix must not break the launch home's own process-configuration load."""
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from hermes_cli.env_loader import load_hermes_dotenv

    launch_home = tmp_path / "launch"
    launch_home.mkdir(parents=True)
    (launch_home / ".env").write_text("HERMES_TEST_LAUNCH_MARKER=launch-value\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.delenv("HERMES_TEST_LAUNCH_MARKER", raising=False)

    token = set_multiplex_context(True)
    try:
        load_hermes_dotenv(hermes_home=launch_home)
    finally:
        reset_multiplex_context(token)

    assert os.environ.get("HERMES_TEST_LAUNCH_MARKER") == "launch-value", (
        "the launch home's own .env must still load under multiplex semantics — it is process "
        "configuration, not a routed profile's credential"
    )


# --------------------------------------------------------------------------------------
# Leak B — a bulk secret source overwrites the profile's own token
# --------------------------------------------------------------------------------------


def test_bulk_source_cannot_override_profile_own_token(tmp_path):
    """A bulk source may not override a name the profile's own ``.env`` defines.

    A bulk source (``secrets.command``) emits ONE canonical value per process. Letting it
    overwrite a profile's own ``.env`` credential aliases one bot's identity into another
    profile's runtime.
    """
    from agent.secret_sources import registry

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=8851168207:AAA-own-bot-secret\n", encoding="utf-8")

    env = {"TELEGRAM_BOT_TOKEN": "8851168207:AAA-own-bot-secret"}
    cfg = {
        "secrets": {
            "sources": ["command"],
            "command": {
                "enabled": True,
                "shape": "bulk",
                "override_existing": True,
                "command": "echo TELEGRAM_BOT_TOKEN=8979456313:AAA-sibling-bot-secret",
            },
        }
    }

    report = registry.apply_all(cfg["secrets"], home, environ=env)

    assert env.get("TELEGRAM_BOT_TOKEN") == "8851168207:AAA-own-bot-secret", (
        "a bulk secret source overwrote the profile's own bot token with a sibling's — "
        "the profile will post as the wrong bot"
    )
    # The refusal must be ATTRIBUTED, not silent.
    assert any("TELEGRAM_BOT_TOKEN" in (sr.skipped_existing or []) for sr in report.sources), (
        "the refusal was not attributed to a source report"
    )


def test_bulk_source_may_still_supply_a_name_the_profile_does_not_define(tmp_path):
    """The bulk guard is narrow: a name the profile does NOT define is still source-managed."""
    from agent.secret_sources import registry

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text("HERMES_TEST_OWN=mine\n", encoding="utf-8")

    env = {"HERMES_TEST_OWN": "mine"}
    cfg = {
        "secrets": {
            "sources": ["command"],
            "command": {
                "enabled": True,
                "shape": "bulk",
                "override_existing": True,
                "command": "echo HERMES_TEST_FROM_BULK=bulk-value",
            },
        }
    }

    registry.apply_all(cfg["secrets"], home, environ=env)

    assert env.get("HERMES_TEST_FROM_BULK") == "bulk-value", (
        "the bulk guard wrongly blocked a name the profile does not define"
    )
    assert env.get("HERMES_TEST_OWN") == "mine"


def test_placeholder_values_stay_source_managed(tmp_path):
    """A PLACEHOLDER value means 'source-managed' — the source stays authoritative for it."""
    from agent.secret_sources import registry

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=__COMMAND_MANAGED__\n", encoding="utf-8"
    )

    env = {"TELEGRAM_BOT_TOKEN": "__COMMAND_MANAGED__"}
    cfg = {
        "secrets": {
            "sources": ["command"],
            "command": {
                "enabled": True,
                "shape": "bulk",
                "override_existing": True,
                "command": "echo TELEGRAM_BOT_TOKEN=8979456313:AAA-sibling-bot-secret",
            },
        }
    }

    registry.apply_all(cfg["secrets"], home, environ=env)

    assert env.get("TELEGRAM_BOT_TOKEN") == "8979456313:AAA-sibling-bot-secret", (
        "a placeholder value must stay source-managed — the bulk source resolves it"
    )


# --------------------------------------------------------------------------------------
# The runtime backstop — resolve the token for the CALLING profile
# --------------------------------------------------------------------------------------


def test_resolve_bot_token_prefers_the_profile_own_env(monkeypatch, tmp_path):
    """The profile's own ``.env`` wins over the ambient environment."""
    from tools import telegram_identity

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=8851168207:AAA-own\n", encoding="utf-8")

    monkeypatch.setattr(telegram_identity, "profile_home", lambda profile=None: home)

    token = telegram_identity.resolve_bot_token()
    assert token == "8851168207:AAA-own"


def test_bot_id_from_token():
    from tools.telegram_identity import bot_id_from_token

    assert bot_id_from_token("8851168207:AAA-secret") == "8851168207"
    assert bot_id_from_token("") is None
    assert bot_id_from_token(None) is None
    assert bot_id_from_token("no-colon") is None
    assert bot_id_from_token("notdigits:secret") is None


def test_check_identity_refuses_another_profiles_bot(monkeypatch, tmp_path):
    """A token that belongs to a sibling profile is REFUSED, not silently used."""
    from tools import telegram_identity

    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=8851168207:AAA-own\n", encoding="utf-8")

    monkeypatch.setattr(telegram_identity, "profile_home", lambda profile=None: home)

    with pytest.raises(telegram_identity.IdentityMismatch):
        telegram_identity.check_identity(token="8979456313:AAA-sibling")
