"""PHASE 3 (t_380891bd): which bot posted — on EVERY outbound Telegram send.

The parent card (t_43eff563) fixed the two credential leaks that let a
``default``-profile worker post as ULTRON's bot. This phase pins the two
observable properties that make such a mis-post impossible to repeat
unnoticed:

* **The bot id is logged on every outbound send.** The bot id is NOT a secret
  (the token is). Before this, the ONLY way to tell which bot spoke was to
  count ``bot<id>/sendMessage`` URLs in the log — which is exactly how the
  t_43eff563 mis-post went unnoticed for a day. One ``sent via bot <id>`` line
  per send makes "Vision said X" and "Ultron said X" distinguishable facts.
* **The gateway adapter path logs it too.** The parent's wiring covered the
  ONE-SHOT send (``plugins.platforms.telegram.standalone``). The GATEWAY
  adapter is the path that actually delivered the mis-posted message to Bryan
  (cron delivery, gateway replies, media, drafts) and it had NO bot-id
  logging at all. That gap is closed here.

The adapter tests drive the real chokepoints with a fake PTB bot, so a
regression that removes the log call fails the test rather than silently
restoring "nobody knows which bot posted".
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pytest

VISION_BOT_ID = "8851168207"
ULTRON_BOT_ID = "8810592884"


# --------------------------------------------------------------------------
# Fixtures — a root (default) profile home with a sibling ``ultron`` profile.
# --------------------------------------------------------------------------

def _write_profile_env(home, token: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={token}:SECRETPART\n"
        f"TELEGRAM_HOME_CHANNEL=8943042074\n",
        encoding="utf-8",
    )


def _token_for(home) -> str:
    for line in (home / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"no TELEGRAM_BOT_TOKEN in {home / '.env'}")


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """A root (default) profile home with a sibling ``ultron`` profile under it."""
    root = tmp_path / ".hermes"
    ultron = root / "profiles" / "ultron"
    _write_profile_env(root, VISION_BOT_ID)
    _write_profile_env(ultron, ULTRON_BOT_ID)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    return root, ultron


@pytest.fixture
def captured_bots(monkeypatch):
    """Capture the bot id each one-shot Telegram request is issued against."""
    import plugins.platforms.telegram.standalone as sms

    created: list = []

    def _fake_telegram_bot(token, extra=None):
        bot = _FakeAdapterBot(str(token).split(":", 1)[0])
        created.append(bot)
        return bot

    monkeypatch.setattr(sms, "_telegram_bot", _fake_telegram_bot)
    monkeypatch.setattr(sms, "_telegram_format", lambda m: (m, None, False))
    return created


# --------------------------------------------------------------------------
# The one-shot send path: plugins/platforms/telegram/standalone.py
# --------------------------------------------------------------------------

def test_standalone_send_logs_the_bot_id(profile_home, captured_bots, caplog):
    """A one-shot send records which bot posted (the bot id is not a secret)."""
    import asyncio

    from plugins.platforms.telegram.standalone import _send_telegram

    root, _ultron = profile_home
    token = _token_for(root)

    with caplog.at_level(logging.INFO, logger="tools.telegram_identity"):
        result = asyncio.run(_send_telegram(token, "8943042074", "hello"))

    assert result.get("message_id") or "message_id" in result, result
    bot_id_lines = [rec.getMessage() for rec in caplog.records if "sent via bot" in rec.getMessage()]
    assert bot_id_lines, "the send did not record which bot posted"
    assert VISION_BOT_ID in bot_id_lines[0]


# --------------------------------------------------------------------------
# The gateway adapter path: plugins/platforms/telegram/adapter.py
# --------------------------------------------------------------------------

class _FakeAdapterBot:
    """Records the bot id each Telegram request was issued against."""

    def __init__(self, bot_id: str = VISION_BOT_ID):
        self.bot_id = bot_id
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append((self.bot_id, {"chat_id": chat_id, "text": text, **kwargs}))
        return type("Msg", (), {"message_id": 1})()

    async def send_message_draft(self, **kwargs):
        self.sent.append((self.bot_id, dict(kwargs)))
        return True

    async def send_photo(self, **kwargs):
        self.sent.append((self.bot_id, dict(kwargs)))
        return type("Msg", (), {"message_id": 2})()


def _make_adapter(bot_id: str = VISION_BOT_ID):
    """A TelegramAdapter with just enough state for the outbound chokepoints.

    The adapter is built with ``object.__new__`` on purpose: the real
    ``__init__`` pulls in PTB, the network, and a dozen subsystems that have
    nothing to do with "which bot id gets logged".
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.config import PlatformConfig

    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(token=f"{bot_id}:SECRETPART")
    adapter._bot = _FakeAdapterBot(bot_id)
    # Attributes the real __init__ sets and the outbound chokepoints read.
    adapter._reply_to_mode = "first"
    adapter._disable_link_previews = False
    return adapter


def test_adapter_resolves_its_own_bot_id(profile_home):
    """The adapter's bot id comes from ITS OWN config token, not from ambient state."""
    adapter = _make_adapter(ULTRON_BOT_ID)

    assert adapter._outbound_bot_id() == ULTRON_BOT_ID


def test_adapter_logs_the_bot_id_on_a_text_send(profile_home, caplog):
    """Every text send through the adapter records which bot posted."""
    import asyncio

    adapter = _make_adapter(VISION_BOT_ID)

    with caplog.at_level(logging.INFO, logger="tools.telegram_identity"):
        asyncio.run(adapter._send_chunk_markdown_or_plain(
            "hello", {"chat_id": 8943042074}, None))

    bot_id_lines = [rec.getMessage() for rec in caplog.records if "sent via bot" in rec.getMessage()]
    assert bot_id_lines, "the adapter's text send did not record which bot posted"
    assert VISION_BOT_ID in bot_id_lines[0]


def test_adapter_logs_the_bot_id_on_a_control_send(profile_home, caplog):
    """A control message (approval prompt, picker) records which bot posted."""
    import asyncio

    adapter = _make_adapter(VISION_BOT_ID)

    with caplog.at_level(logging.INFO, logger="tools.telegram_identity"):
        asyncio.run(adapter._send_message_with_thread_fallback(
            chat_id=8943042074, text="pick one"))

    bot_id_lines = [rec.getMessage() for rec in caplog.records if "sent via bot" in rec.getMessage()]
    assert bot_id_lines, "the adapter's control send did not record which bot posted"
    assert VISION_BOT_ID in bot_id_lines[0]


def test_adapter_logs_the_bot_id_on_a_media_send(profile_home, caplog):
    """A media send (photo, voice, document) records which bot posted."""
    import asyncio

    adapter = _make_adapter(VISION_BOT_ID)

    async def _fake_retry(*args, **kwargs):
        return type("Msg", (), {"message_id": 3})()

    adapter._send_with_dm_topic_reply_anchor_retry = _fake_retry

    with caplog.at_level(logging.INFO, logger="tools.telegram_identity"):
        asyncio.run(adapter._send_media(
            adapter._bot.send_photo, 8943042074, None, None, "photo", photo="http://x/y.png"))

    bot_id_lines = [rec.getMessage() for rec in caplog.records if "sent via bot" in rec.getMessage()]
    assert bot_id_lines, "the adapter's media send did not record which bot posted"
    assert VISION_BOT_ID in bot_id_lines[0]


def test_adapter_logs_the_bot_id_on_a_draft(profile_home, caplog):
    """A streaming draft records which bot posted."""
    import asyncio

    from plugins.platforms.telegram import telegram_flood_window

    adapter = _make_adapter(VISION_BOT_ID)
    # The draft path needs a few adapter attributes the real __init__ would set.
    adapter._rich_messages_enabled = False
    adapter._rich_drafts_enabled = False
    adapter._reply_to_mode = "first"

    with caplog.at_level(logging.INFO, logger="tools.telegram_identity"):
        asyncio.run(telegram_flood_window.send_draft(adapter, 8943042074, 1, "typing…", None))

    bot_id_lines = [rec.getMessage() for rec in caplog.records if "sent via bot" in rec.getMessage()]
    assert bot_id_lines, "the adapter's draft did not record which bot posted"
    assert VISION_BOT_ID in bot_id_lines[0]


def test_adapter_bot_id_survives_a_token_with_no_numeric_id(profile_home):
    """A self-hosted Bot API server's token has no numeric id — the id is simply absent.

    The bot id is only LOGGED. Refusing such a send would break a legitimate
    deployment, so the helper returns None and the send still proceeds.
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.config import PlatformConfig

    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(token="not-a-token")

    assert adapter._outbound_bot_id() is None


def test_adapter_bot_id_is_none_when_the_adapter_has_no_token(profile_home):
    """An adapter with no token logs "<unknown>" rather than inventing an id."""
    import asyncio

    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.config import PlatformConfig

    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(token=None)
    adapter._bot = _FakeAdapterBot()

    assert adapter._outbound_bot_id() is None
    # The log call must not raise on a missing id.
    adapter._note_outbound_send(8943042074)
