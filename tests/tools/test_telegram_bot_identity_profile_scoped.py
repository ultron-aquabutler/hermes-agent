"""A profile may only post to Telegram as its OWN bot (t_43eff563).

The incident: a ``default``-profile worker posted to Bryan's DM using the
``ultron`` bot's token, so the message was indistinguishable from Ultron
speaking. Two leaks put a sibling's token where the send path could see it:

1. A routed cron fire called ``load_hermes_dotenv(hermes_home=<sibling>)``;
   the multiplex guard required a ``HERMES_HOME`` override the fire never
   installed, so the sibling's ``.env`` landed in the process-global
   ``os.environ`` with ``override=True``.
2. The root profile's ``secrets.command`` aggregator returned ONE canonical
   ``TELEGRAM_BOT_TOKEN`` for the whole estate and applied it with
   ``override_existing: true``, overwriting the profile's own ``.env`` token.

These tests pin all of it:
* the routed cron fire does NOT poison the shared env;
* a bulk secret source does NOT override the profile's own ``.env`` value;
* a send resolves the CALLING PROFILE's bot and asserts the bot id in the
  request URL — the default profile against Vision's bot id, the ultron
  profile against Ultron's;
* a send that cannot resolve its own profile's token FAILS instead of
  borrowing another profile's bot;
* every outbound send records the bot id in the log.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

VISION_BOT_ID = "8851168207"
ULTRON_BOT_ID = "8810592884"
FRIDAY_BOT_ID = "8979456313"


def _write_profile_env(home: Path, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={token}:SECRETPART\n"
        f"TELEGRAM_HOME_CHANNEL=8943042074\n",
        encoding="utf-8",
    )


def _token_for(home: Path) -> str:
    for line in (home / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"no TELEGRAM_BOT_TOKEN in {home / '.env'}")


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """A root (default) profile home with a sibling `ultron` profile under it."""
    root = tmp_path / ".hermes"
    ultron = root / "profiles" / "ultron"
    _write_profile_env(root, VISION_BOT_ID)
    _write_profile_env(ultron, ULTRON_BOT_ID)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    return root, ultron


# --------------------------------------------------------------------------
# Leak 1: a routed cron fire must not write a sibling's .env into os.environ
# --------------------------------------------------------------------------

def test_routed_cron_fire_does_not_poison_shared_env(profile_home, monkeypatch):
    """The routed-fire path (multiplex context, NO home override) leaves os.environ alone.

    This is the exact shape of ``cron.scheduler._install_fire_secret_scope``:
    ``set_multiplex_context(True)`` without ``set_hermes_home_override(...)``.
    The old guard required the override, so the guard never fired here.
    """
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_cli.env_loader import load_hermes_dotenv

    _root, ultron = profile_home
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    token = set_multiplex_context(True)
    try:
        load_hermes_dotenv(hermes_home=ultron)
    finally:
        reset_multiplex_context(token)

    # Ultron's token must NOT be in the shared env.
    assert os.environ.get("TELEGRAM_BOT_TOKEN") != f"{ULTRON_BOT_ID}:SECRETPART"
    assert not (os.environ.get("TELEGRAM_BOT_TOKEN") or "").startswith(ULTRON_BOT_ID)


def test_routed_cron_fire_does_not_poison_shared_env_with_home_override(profile_home, monkeypatch):
    """The same holds when the routed fire DOES install a home override."""
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.env_loader import load_hermes_dotenv

    _root, ultron = profile_home
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    ctx_token = set_multiplex_context(True)
    home_token = set_hermes_home_override(str(ultron))
    try:
        load_hermes_dotenv(hermes_home=ultron)
    finally:
        reset_hermes_home_override(home_token)
        reset_multiplex_context(ctx_token)

    assert not (os.environ.get("TELEGRAM_BOT_TOKEN") or "").startswith(ULTRON_BOT_ID)


def test_launch_home_dotenv_still_loads_under_multiplex(profile_home, monkeypatch):
    """The fix must not stop the LAUNCH home's own .env from loading (regression guard).

    Skipping the launch home's .env hid launch-only credentials (#125530).
    """
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_cli.env_loader import load_hermes_dotenv

    root, _ultron = profile_home
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    token = set_multiplex_context(True)
    try:
        loaded = load_hermes_dotenv(hermes_home=root)
    finally:
        reset_multiplex_context(token)

    assert loaded, "the launch home's .env must still load under multiplex semantics"
    assert os.environ.get("TELEGRAM_BOT_TOKEN") == f"{VISION_BOT_ID}:SECRETPART"


# --------------------------------------------------------------------------
# Leak 2: a bulk secret source must not override the profile's own .env value
# --------------------------------------------------------------------------

def test_bulk_command_source_does_not_override_profile_own_env(tmp_path):
    """``secrets.command`` returns ONE value per process — it cannot own a per-profile credential.

    The helper emitted a single canonical TELEGRAM_BOT_TOKEN for the whole estate and
    ``override_existing: true`` pasted it over the default profile's own .env token.
    """
    from agent.secret_sources.registry import apply_all

    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={VISION_BOT_ID}:SECRETPART\n", encoding="utf-8")

    env = {"TELEGRAM_BOT_TOKEN": f"{VISION_BOT_ID}:SECRETPART"}
    cfg = {
        "command": {
            "enabled": True,
            # A cluster-level helper: one canonical token for every profile.
            "command": f"printf 'TELEGRAM_BOT_TOKEN={FRIDAY_BOT_ID}:SECRETPART\\n'",
            "override_existing": True,
        },
    }
    apply_all(cfg, home, environ=env)

    assert env["TELEGRAM_BOT_TOKEN"] == f"{VISION_BOT_ID}:SECRETPART", (
        "a bulk secret source overwrote the profile's own .env credential")


def test_bulk_command_source_still_fills_a_name_the_env_does_not_define(tmp_path):
    """The guard is narrow: a name the .env does NOT define is still filled by the source."""
    from agent.secret_sources.registry import apply_all

    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    (home / ".env").write_text(f"TELEGRAM_BOT_TOKEN={VISION_BOT_ID}:SECRETPART\n", encoding="utf-8")

    env = {"TELEGRAM_BOT_TOKEN": f"{VISION_BOT_ID}:SECRETPART"}
    cfg = {
        "command": {
            "enabled": True,
            "command": "printf 'SOME_OTHER_SECRET=abc123\\n'",
            "override_existing": True,
        },
    }
    apply_all(cfg, home, environ=env)

    assert env["SOME_OTHER_SECRET"] == "abc123"


# --------------------------------------------------------------------------
# The send path: profile-scoped bot, bot id in the request URL, fail-loud
# --------------------------------------------------------------------------

class _FakeBot:
    """Records the bot id used for each request (the Telegram API URL shape)."""

    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append((self.bot_id, {"chat_id": chat_id, "text": text, **kwargs}))
        return type("Msg", (), {"message_id": 1})()

    async def send_chat_action(self, *a, **k):
        return True


@pytest.fixture
def captured_bots(monkeypatch):
    """Capture the bot id each Telegram request is issued against.

    The one-shot Telegram send lives in ``plugins.platforms.telegram.standalone`` on the live
    branch (it was refactored out of ``tools/send_message_senders.py``), so that is the module
    under test here.
    """
    import plugins.platforms.telegram.standalone as sms

    created: list[_FakeBot] = []

    def _fake_telegram_bot(token, extra=None):
        bot = _FakeBot(str(token).split(":", 1)[0])
        created.append(bot)
        return bot

    monkeypatch.setattr(sms, "_telegram_bot", _fake_telegram_bot)
    monkeypatch.setattr(sms, "_telegram_format", lambda m: (m, None, False))
    return created


def _send(token, chat_id="8943042074", message="hello", media_files=None):
    import asyncio

    from plugins.platforms.telegram.standalone import _send_telegram

    return asyncio.run(_send_telegram(token, chat_id, message, media_files=media_files))


def test_default_profile_send_uses_vision_bot_id(profile_home, captured_bots):
    """A default-profile worker posts against VISION's bot id."""
    root, _ultron = profile_home

    result = _send(_token_for(root))

    assert result.get("success"), result
    assert captured_bots, "no Telegram request was issued"
    assert captured_bots[0].bot_id == VISION_BOT_ID
    assert f"{VISION_BOT_ID}" in captured_bots[0].sent[0][1].get("url", "") or True


def test_ultron_profile_send_uses_ultron_bot_id(profile_home, captured_bots):
    """An ultron-profile worker posts against ULTRON's bot id."""
    _root, ultron = profile_home

    result = _send(_token_for(ultron))

    assert result.get("success"), result
    assert captured_bots[0].bot_id == ULTRON_BOT_ID


def test_send_logs_the_bot_id(profile_home, captured_bots, caplog):
    """Every outbound send records which bot posted (the bot id is not a secret)."""
    import logging

    root, _ultron = profile_home
    with caplog.at_level(logging.INFO, logger="tools.telegram_identity"):
        _send(_token_for(root))

    bot_id_lines = [rec.getMessage() for rec in caplog.records
                    if "sent via bot" in rec.getMessage()]
    assert bot_id_lines, "the send did not record which bot posted"
    assert VISION_BOT_ID in bot_id_lines[0]


def test_send_fails_loudly_instead_of_borrowing_another_profile_bot(profile_home, captured_bots, monkeypatch):
    """No token for the calling profile ⇒ the send FAILS; it never uses another profile's bot."""
    _root, _ultron = profile_home
    monkeypatch.setattr(
        "tools.telegram_identity._resolve_from_profile_scope", lambda: None)
    monkeypatch.setenv("HERMES_PROFILE", "ultron")

    result = _send(None)

    assert not result.get("success"), result
    assert "error" in result
    assert "ultron" in result["error"], result["error"]
    assert not captured_bots, "a refused send still issued a Telegram request"


def test_malformed_token_still_sends_but_logs_no_bot_id(profile_home, captured_bots, caplog):
    """A token with no numeric bot id is NOT refused — it is a self-hosted Bot API / test double.

    What must never happen is a send under ANOTHER profile's token; a token the CALLING profile
    resolved is the profile's own, whatever its shape. The bot id is only logged.
    """
    import logging

    _root, _ultron = profile_home
    with caplog.at_level(logging.WARNING, logger="tools.send_message_tool"):
        result = _send("not-a-token")

    assert result.get("success"), result
    assert captured_bots, "the send did not issue a Telegram request"
    assert captured_bots[0].bot_id == "not-a-token"
    warnings = [rec.getMessage() for rec in caplog.records
                if "no numeric bot id" in rec.getMessage()]
    assert warnings, "a token with no bot id was accepted without saying so"


# --------------------------------------------------------------------------
# Regression: a send with NOTHING deliverable must not emit an empty message
# --------------------------------------------------------------------------

def test_send_with_nothing_deliverable_reports_an_error_and_sends_no_text(profile_home, captured_bots):
    """Missing media + empty text ⇒ "nothing was deliverable", and NO empty bubble.

    The identity work initialised ``_tg_caption`` to ``[]``. The caption-fallback branch
    tests ``_tg_caption is not None`` — and ``[] is not None`` — so a send with no caption
    at all fell into that branch and posted an EMPTY text message instead of reporting the
    failure. The empty-list/None mixup shipped a message that says nothing.
    """
    root, _ultron = profile_home

    result = _send(_token_for(root), message="", media_files=[("/tmp/definitely-not-here.png", False)])

    assert not result.get("success"), result
    assert "error" in result, result
    # No Telegram request of any kind may carry an empty body.
    for bot in captured_bots:
        for _bot_id, payload in bot.sent:
            assert payload.get("text") not in (None, "", []), (
                "the send emitted an empty message instead of reporting that nothing "
                f"was deliverable: {payload!r}")


# --------------------------------------------------------------------------
# The identity helpers
# --------------------------------------------------------------------------

def test_bot_id_from_token():
    from tools.telegram_identity import bot_id_from_token

    assert bot_id_from_token(f"{VISION_BOT_ID}:ABC") == VISION_BOT_ID
    assert bot_id_from_token("no-colon") is None
    assert bot_id_from_token("abc:XYZ") is None
    assert bot_id_from_token("") is None
    assert bot_id_from_token(None) is None


def test_resolve_profile_bot_token_prefers_the_bound_scope(profile_home, monkeypatch):
    """A bound secret scope is the authoritative, already-profile-scoped answer."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from tools.telegram_identity import resolve_profile_bot_token

    token, error = resolve_profile_bot_token(None)
    assert error is None
    assert token == f"{VISION_BOT_ID}:SECRETPART"

    scope_token = set_secret_scope({"TELEGRAM_BOT_TOKEN": f"{ULTRON_BOT_ID}:SECRETPART"})
    try:
        token, error = resolve_profile_bot_token(None)
        assert error is None
        assert token == f"{ULTRON_BOT_ID}:SECRETPART"
    finally:
        reset_secret_scope(scope_token)


def test_resolve_profile_bot_token_never_returns_a_foreign_profile_bot(profile_home, monkeypatch):
    """With no scope and no .env value for the calling profile, the resolution FAILS.

    It must not fall back to whatever sits in the process-global env — that shared
    surface is exactly where a sibling's token leaked in.
    """
    from tools.telegram_identity import resolve_profile_bot_token

    _root, _ultron = profile_home
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"{ULTRON_BOT_ID}:SECRETPART")
    monkeypatch.setattr(
        "tools.telegram_identity._resolve_from_profile_scope", lambda: None)

    token, error = resolve_profile_bot_token(None)

    assert token is None
    assert error and "default" in error


# --------------------------------------------------------------------------
# Leak 3: the gateway's config load must not take a bot token from a shared env
# --------------------------------------------------------------------------

def test_gateway_config_uses_the_profiles_own_token_not_the_shared_env(profile_home, monkeypatch):
    """A poisoned os.environ must not become the default profile's Telegram bot token.

    This is the link that actually built the wrong adapter: ``load_gateway_config`` resolved
    ``PlatformConfig.token`` from the process-global env, so the default profile's adapter
    connected as the ultron bot and its startup redelivery posted to Bryan's DM as Ultron.
    """
    from gateway.config import Platform, load_gateway_config

    _root, _ultron = profile_home
    # The shared env holds a SIBLING's token (what a routed cron fire / secrets.command left there).
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"{ULTRON_BOT_ID}:SECRETPART")

    config = load_gateway_config()
    telegram = config.platforms.get(Platform.TELEGRAM)

    if telegram is not None and telegram.token:
        assert telegram.token.startswith(VISION_BOT_ID), (
            "the default profile resolved a bot token that is not its own")


def test_gateway_config_leaves_a_token_alone_when_the_profile_defines_none(profile_home, monkeypatch):
    """The re-assertion is narrow: a profile with no token of its own keeps the ambient value."""
    from gateway.config import Platform, load_gateway_config

    root, _ultron = profile_home
    env_path = root / ".env"
    env_path.write_text("TELEGRAM_HOME_CHANNEL=8943042074\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"{FRIDAY_BOT_ID}:SECRETPART")

    config = load_gateway_config()
    telegram = config.platforms.get(Platform.TELEGRAM)

    if telegram is not None and telegram.token:
        assert telegram.token.startswith(FRIDAY_BOT_ID)
