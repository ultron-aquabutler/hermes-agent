"""Which bot posted — on EVERY outbound Telegram send (the gateway adapter path).

The parent fix (t_02ebb908) closed the two credential leaks that let a ``default``-profile
worker post to Bryan's DM as the ``ultron`` bot (t_43eff563). This pins the OBSERVABLE
property that makes such a mis-post impossible to repeat unnoticed:

* **The bot id is logged on every outbound send.** The bot id is NOT a secret (the token is).
  Before this, the ONLY way to tell which bot spoke was to count ``bot<id>/sendMessage``
  URLs in the log — which is exactly how the t_43eff563 mis-post went unnoticed for a day.
  One ``bot_id=`` per send makes "Friday said X" and "Ultron said X" distinguishable facts.

The GATEWAY adapter is the path that actually delivered the mis-posted message to Bryan
(cron delivery, gateway replies, media, drafts). These tests drive the REAL send chokepoints
with a fake PTB bot, so a regression that removes the log call fails the test rather than
silently restoring "nobody knows which bot posted".

The bot id is derived from the adapter's OWN ``config.token`` — the token the profile's
config resolved — so the logged id is the identity this adapter actually authenticates with.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter

# The live bot ids (read from each profile's own .env on 2026-11-17). The bot id is the
# public half of the credential — safe to name in a test.
DEFAULT_BOT_ID = "8851168207"
ULTRON_BOT_ID = "8810592884"


def _make_adapter(token: str = f"{DEFAULT_BOT_ID}:SECRETPART") -> TelegramAdapter:
    """A TelegramAdapter with a fake PTB bot wired for the send path."""
    config = PlatformConfig(enabled=True, token=token)
    adapter = TelegramAdapter(config)
    bot = MagicMock()
    bot.do_api_request = AsyncMock(return_value=SimpleNamespace(message_id=123))
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2))
    bot.send_voice = AsyncMock(return_value=MagicMock(message_id=3))
    bot.send_chat_action = AsyncMock()
    bot.send_message_draft = AsyncMock(return_value=True)
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=1))
    bot.delete_message = AsyncMock(return_value=True)
    bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=77))
    adapter._bot = bot
    return adapter


def _log_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_text_send_logs_the_bot_id(caplog):
    """A text send names the bot it posted as."""
    adapter = _make_adapter()

    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
        await adapter._send_chunk_markdown_or_plain(
            "hello", {"chat_id": -100123}, parse_mode=None)

    text = _log_text(caplog)
    assert f"bot_id={DEFAULT_BOT_ID}" in text, f"the outbound send did not name the bot id: {text}"


@pytest.mark.asyncio
async def test_the_bot_token_never_appears_in_the_log(caplog):
    """The bot id is loggable; the TOKEN is not."""
    adapter = _make_adapter(f"{DEFAULT_BOT_ID}:SUPERSECRETPART")

    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
        await adapter._send_chunk_markdown_or_plain(
            "hello", {"chat_id": -100123}, parse_mode=None)

    text = _log_text(caplog)
    assert "SUPERSECRETPART" not in text, "the bot TOKEN leaked into the outbound log"


@pytest.mark.asyncio
async def test_each_profile_logs_its_OWN_bot_id(caplog):
    """Two adapters, two identities, two distinct log lines.

    This is the property the mis-post violated: the bot id in the log says WHICH profile
    spoke, so two bots in one chat are distinguishable.
    """
    default_adapter = _make_adapter(f"{DEFAULT_BOT_ID}:SECRETPART")
    ultron_adapter = _make_adapter(f"{ULTRON_BOT_ID}:SECRETPART")

    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
        await default_adapter._send_chunk_markdown_or_plain(
            "from default", {"chat_id": -100123}, parse_mode=None)
        await ultron_adapter._send_chunk_markdown_or_plain(
            "from ultron", {"chat_id": -100123}, parse_mode=None)

    text = _log_text(caplog)
    assert f"bot_id={DEFAULT_BOT_ID}" in text
    assert f"bot_id={ULTRON_BOT_ID}" in text


@pytest.mark.asyncio
async def test_the_adapter_logs_its_own_config_token_not_an_ambient_one(caplog):
    """The logged id comes from the adapter's own config, not from the process env.

    A sibling profile's token in ``os.environ`` must not change what THIS adapter reports —
    the adapter authenticates with ``config.token`` and must report that.
    """
    import os

    adapter = _make_adapter(f"{DEFAULT_BOT_ID}:SECRETPART")
    os.environ["TELEGRAM_BOT_TOKEN"] = f"{ULTRON_BOT_ID}:AMBIENTSECRET"
    try:
        with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
            await adapter._send_chunk_markdown_or_plain(
                "hello", {"chat_id": -100123}, parse_mode=None)
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    text = _log_text(caplog)
    assert f"bot_id={DEFAULT_BOT_ID}" in text
    assert f"bot_id={ULTRON_BOT_ID}" not in text, (
        "the adapter logged the AMBIENT token's bot id instead of the one it authenticates with"
    )


@pytest.mark.asyncio
async def test_topic_seed_logs_the_bot_id(caplog):
    """A DM topic creation names the bot too."""
    adapter = _make_adapter()

    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
        await adapter._create_dm_topic(chat_id=8943042074, name="a topic")

    text = _log_text(caplog)
    assert f"bot_id={DEFAULT_BOT_ID}" in text, f"the topic seed did not name the bot id: {text}"


def test_a_bot_id_is_not_logged_when_the_token_is_unusable(caplog):
    """A self-hosted Bot API token (no numeric id) logs ``unknown``, never a crash."""
    adapter = _make_adapter("local-bot-api-token-without-a-colon")

    with caplog.at_level(logging.INFO, logger="plugins.platforms.telegram.adapter"):
        adapter._note_outbound_send(-100123, "message")

    text = _log_text(caplog)
    assert "bot_id=unknown" in text, f"expected an unknown bot id, got: {text}"


def test_the_cached_bot_id_is_none_for_an_unusable_token():
    """The cache stores a 1-tuple so a ``None`` id is a real answer, not a cache miss."""
    adapter = _make_adapter("local-bot-api-token-without-a-colon")

    assert adapter._outbound_bot_id() is None
    # Second call reads the cache — it must still answer None, not the tuple.
    assert adapter._outbound_bot_id() is None


def test_the_bot_id_is_resolved_from_the_adapter_config():
    """The id comes from the adapter's own config token."""
    adapter = _make_adapter(f"{ULTRON_BOT_ID}:SECRETPART")

    assert adapter._outbound_bot_id() == ULTRON_BOT_ID
