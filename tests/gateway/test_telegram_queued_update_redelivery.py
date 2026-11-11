"""Queued-update redelivery after a poll stall / cold boot (#11023).

A message a human sent while the Telegram adapter was not polling is a message they
are waiting on. Two properties are pinned here:

1. A connection that PRESERVES Telegram's pending queue counts what comes back and
   logs ``redelivered N queued update(s) after stall`` — the loss is never silent.
2. A simulated stall during an inbound DM results in the message being REDELIVERED
   (the queue is preserved, the update is admitted and dispatched) rather than dropped.

The complementary guarantee — a preserved backlog does not double-reply — is provided
by the persistent per-bot receipt file
(``plugins/platforms/telegram/update_admission.py``): a redelivered update whose receipt
is on disk is dropped by admission, so preserving the queue cannot replay an answered
message.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter

_ADAPTER_LOGGER = "plugins.platforms.telegram.adapter"
_REDELIVERED = "redelivered"


def _make_adapter(extra=None) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra=extra or {}))
    adapter._webhook_mode = False
    adapter._begin_polling_generation()
    return adapter


def _receive(adapter: TelegramAdapter, n: int) -> None:
    """Simulate n updates arriving on the getUpdates wire for the current generation."""
    request = MagicMock()
    request.parse_json_payload = MagicMock(
        return_value={"ok": True, "result": [{"update_id": i} for i in range(n)]}
    )
    adapter._observe_polling_request_result(request, adapter._polling_generation, (200, b"{}"))


def _redelivery_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if _REDELIVERED in r.message]


@pytest.mark.asyncio
async def test_cold_boot_preserving_arms_redelivery_counter(caplog):
    """A cold boot that preserves the queue arms the counter before any update arrives."""
    adapter = _make_adapter()
    assert adapter._queued_updates_preserved is False

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        assert adapter._cold_boot_drop_pending(is_reconnect=False) is False

    assert adapter._queued_updates_preserved is True
    assert adapter._redelivered_updates_count == 0
    # Nothing was redelivered yet, so nothing is claimed.
    assert _redelivery_lines(caplog) == []


@pytest.mark.asyncio
async def test_cold_boot_dropping_does_not_arm_redelivery_counter():
    """Opting back into the discard behaviour must not claim a redelivery that never happened."""
    adapter = _make_adapter(extra={"drop_pending_on_cold_boot": True})

    assert adapter._cold_boot_drop_pending(is_reconnect=False) is True
    assert adapter._queued_updates_preserved is False


@pytest.mark.asyncio
async def test_redelivered_updates_are_counted_and_logged(caplog):
    """Updates handed back by Telegram after a preserved-queue connect are counted and logged."""
    adapter = _make_adapter()
    adapter._arm_queued_update_redelivery()

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        _receive(adapter, 3)

    assert adapter._redelivered_updates_count == 3
    lines = _redelivery_lines(caplog)
    assert len(lines) == 1, lines
    assert "redelivered 3 queued update(s) after stall" in lines[0]


@pytest.mark.asyncio
async def test_redelivery_logged_once_per_episode(caplog):
    """A burst of preserved updates produces ONE log line, not one per batch."""
    adapter = _make_adapter()
    adapter._arm_queued_update_redelivery()

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        _receive(adapter, 2)
        _receive(adapter, 5)

    assert adapter._redelivered_updates_count == 7
    assert len(_redelivery_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_no_redelivery_claimed_when_queue_was_dropped(caplog):
    """With the discard behaviour opted into, a late update is not reported as redelivered."""
    adapter = _make_adapter(extra={"drop_pending_on_cold_boot": True})
    adapter._cold_boot_drop_pending(is_reconnect=False)

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        _receive(adapter, 4)

    assert adapter._redelivered_updates_count == 0
    assert _redelivery_lines(caplog) == []


@pytest.mark.asyncio
async def test_stall_during_inbound_dm_redelivers_instead_of_dropping(caplog):
    """Regression (#11023): a stall during an inbound DM must not lose the message.

    The scenario: Bryan's DM reaches the adapter, the poll stalls, the supervisor
    rebuilds the adapter. The rebuilt connection PRESERVES Telegram's pending queue, so
    the update comes back on the wire and is counted as redelivered — the operator sees
    ``redelivered N queued update(s) after stall`` in the journal instead of the message
    vanishing.
    """
    adapter = _make_adapter()

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        # The stall hands the adapter to the supervisor, which rebuilds it. The rebuild
        # is a reconnect: the queue is preserved and the redelivery counter is armed.
        adapter._arm_queued_update_redelivery()

        # Telegram hands back the DM that was queued while the adapter was not polling.
        _receive(adapter, 1)

    assert adapter._redelivered_updates_count == 1
    lines = _redelivery_lines(caplog)
    assert len(lines) == 1, lines
    assert "redelivered 1 queued update(s) after stall" in lines[0]
    # The message is NOT dropped: the update counter advanced, so the update reached us.
    assert adapter._updates_received_total >= 1


# --- The full DM path: the message must be ANSWERED, not merely counted. ---


def _make_dm_adapter() -> TelegramAdapter:
    """An adapter wired for the inbound DM path (batching → dispatch)."""
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._webhook_mode = False
    adapter._running = True
    adapter._text_batch_delay_seconds = 0.01
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._pending_photo_batches = {}
    adapter._pending_photo_batch_tasks = {}
    adapter._media_group_events = {}
    adapter._media_group_tasks = {}
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._held_inbound_events = []
    adapter._held_inbound_redispatch_task = None
    adapter._drop_delayed_deliveries = False
    adapter._app = None
    adapter._bot = None
    adapter._set_status_indicator = AsyncMock()
    adapter._release_platform_lock = lambda: None
    adapter.handle_message = AsyncMock()
    adapter._begin_polling_generation()
    return adapter


def _dm_update(update_id: int = 4242, text: str = "Friday, are you there?"):
    """A realistic PTB Update carrying a DM from Bryan (chat_type=private).

    Built with SimpleNamespace rather than the real ``telegram`` classes: the gateway
    test suite installs a ``telegram`` mock in ``sys.modules`` (tests/gateway/conftest.py),
    so ``telegram.Update(...)`` would hand back a MagicMock whose every attribute is a
    MagicMock — the DM would carry a mock text and a mock chat id, and the assertions
    below would prove nothing.
    """
    message = SimpleNamespace(
        message_id=update_id,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        chat=SimpleNamespace(id=8943042074, type="private", title=None, is_forum=False),
        from_user=SimpleNamespace(id=8943042074, full_name="Bryan Wallace", first_name="Bryan"),
        reply_to_message=None,
        date=None,
        location=None,
        photo=None,
        video=None,
        audio=None,
        voice=None,
        document=None,
        sticker=None,
        media_group_id=None,
    )
    return SimpleNamespace(update_id=update_id, message=message, effective_message=message)


@pytest.mark.asyncio
async def test_stall_during_inbound_dm_redelivers_and_answers_the_dm(caplog):
    """AC#5: a simulated adapter stall during an inbound DM → the DM is redelivered AND answered.

    The counting test above proves the redelivery COUNTER moves. It does not prove the
    human's message was ANSWERED — and "counted but never delivered" is the exact failure
    this card exists to kill. So this test drives the real inbound DM path:

      1. Bryan's DM reaches the adapter's PTB handler and is batched for dispatch.
      2. The poll stalls; the supervisor rebuilds the adapter. The rebuild is a
         RECONNECT, so Telegram's pending queue is preserved and the redelivery
         counter is armed.
      3. Telegram hands the queued DM back on the getUpdates wire — the adapter
         counts it (``redelivered 1 queued update(s) after stall``).
      4. PTB dispatches that same redelivered update to the adapter's handler, and
         the DM is dispatched onward — i.e. ANSWERED, not dropped.
    """
    adapter = _make_dm_adapter()
    update = _dm_update()

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        # (1) The DM reaches the adapter and enters the batching lane.
        await adapter._handle_text_message(update, None)
        await asyncio.sleep(0.05)
        adapter.handle_message.assert_called_once()
        answered_before_stall = adapter.handle_message.call_count

        # (2) The poll stalls; the supervisor rebuilds the adapter. The rebuild is a
        # reconnect → the queue is preserved and the redelivery counter is armed.
        assert adapter._cold_boot_drop_pending(is_reconnect=True) is False
        assert adapter._queued_updates_preserved is True

        # (3) Telegram hands the queued DM back on the wire.
        _receive(adapter, 1)

        # (4) PTB dispatches the redelivered update to the adapter's handler, and
        # the DM is dispatched onward — this is the "answered" leg.
        await adapter._handle_text_message(update, None)
        await asyncio.sleep(0.05)

    # The stall did not swallow the message: it came back and was dispatched.
    assert adapter._redelivered_updates_count == 1
    assert adapter.handle_message.call_count == answered_before_stall + 1
    redelivered = adapter.handle_message.call_args[0][0]
    assert redelivered.text == "Friday, are you there?"
    assert redelivered.source.chat_type == "dm"

    # And the loss was never silent.
    lines = _redelivery_lines(caplog)
    assert len(lines) == 1, lines
    assert "redelivered 1 queued update(s) after stall" in lines[0]


@pytest.mark.asyncio
async def test_stall_during_inbound_dm_does_not_answer_a_dropped_queue(caplog):
    """The other side of the same coin: a DISCARDED queue must not claim a redelivery.

    If an operator opts back into ``drop_pending_on_cold_boot``, the queued DM really is
    gone — so the adapter must not log a redelivery that never happened, and must not
    claim a second copy of a message it never received.
    """
    adapter = _make_dm_adapter()
    await adapter._handle_text_message(_dm_update(), None)
    await asyncio.sleep(0.05)
    dispatched_before = adapter.handle_message.call_count

    with caplog.at_level(logging.INFO, logger=_ADAPTER_LOGGER):
        # A cold boot that DISCARDS the queue: the DM is gone, not redelivered.
        adapter._drop_pending_on_cold_boot = True
        assert adapter._cold_boot_drop_pending(is_reconnect=False) is True
        _receive(adapter, 1)
        await asyncio.sleep(0.05)

    assert adapter._redelivered_updates_count == 0
    assert _redelivery_lines(caplog) == []
    assert adapter.handle_message.call_count == dispatched_before
