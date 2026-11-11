"""PTB's shutdown-cleanup getUpdates must not 409 against the replacement poller (t_ff3caccf).

PTB's ``Updater.stop()`` finishes with ``_stop_polling()``, which awaits a private
``__polling_cleanup_cb`` — one last ``get_updates(offset=last_update_id, timeout=0)`` whose
only job is to mark already-fetched updates as "read" so the SAME Updater does not refetch
them on its next ``start_polling()``.

On the adapter-rebuild path that receipt is both worthless and actively harmful:

* Worthless — the Updater is being discarded and its replacement is a DIFFERENT Updater
  with its own ``_last_update_id``. PTB says so itself: "When polling for updates is
  restarted, updates may be fetched again."
* Harmful — the gateway hands a fatal adapter to the reconnect watcher BEFORE awaiting
  ``disconnect()``, so the replacement poller can already be live on the same bot token.
  The cleanup call then lands on the replacement's getUpdates session and self-inflicts a
  409 (``telegram.error.Conflict: terminated by other getUpdates request``).

The live evidence (2026-11-11 05:26, root adapter, journal + gateway.log):

    05:26:39,697  telegram queued for background reconnection
    05:26:39,915  Reconnecting telegram (attempt 1)...
    05:26:41,049  Connected to Telegram (polling mode)      ← replacement poller live
    05:26:44,543  telegram.error.Conflict                    ← the cleanup call, 3.5 s later
    05:26:44,604  Disconnected from Telegram

These tests pin the fix: the discarded Updater must not issue a getUpdates at all, so the
replacement's session is never disturbed.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))


class _Conflict(Exception):
    """Stand-in for ``telegram.error.Conflict``."""


class _FakeBotToken:
    """A bot token whose getUpdates session is held by a REPLACEMENT poller.

    A getUpdates on this token while the replacement holds the session is what Telegram
    answers with ``telegram.error.Conflict``. Model that faithfully so the test fails for
    the real reason (a conflict was raised), not because a mock was misconfigured.
    """

    def __init__(self):
        self.replacement_polling = False
        self.conflicts_raised = 0
        self.cleanup_calls = 0

    async def get_updates(self, **kwargs):
        self.cleanup_calls += 1
        if self.replacement_polling:
            self.conflicts_raised += 1
            raise _Conflict("Conflict: terminated by other getUpdates request; "
                            "make sure that only one bot instance is running")
        return []


class _FakePTBUpdater:
    """Mimic PTB 22.x ``Updater.stop()``: after the polling task quiesces, run the private
    read-receipt callback (a ``get_updates(timeout=0)`` on the same request pool).

    Faithful to ``telegram/ext/_updater.py``'s ``_stop_polling()``: the callback is invoked
    whenever it is set, and its return value is awaited.
    """

    def __init__(self, cleanup_cb):
        self.running = True
        self.cleanup_callbacks_invoked = 0
        if cleanup_cb is not None:
            self._Updater__polling_cleanup_cb = cleanup_cb

    async def stop(self):
        self.running = False
        cb = getattr(self, "_Updater__polling_cleanup_cb", None)
        if cb is not None:
            self.cleanup_callbacks_invoked += 1
            await cb()
            self._Updater__polling_cleanup_cb = None


class _FakeApp:
    """A minimal stand-in for PTB's ``Application`` — every teardown step is awaitable."""

    def __init__(self, updater):
        self.updater = updater
        self.bot = MagicMock()
        self.bot._request = ()
        self.running = True
        self.stopped = False
        self.shutdown_done = False

    async def stop(self):
        self.running = False
        self.stopped = True

    async def shutdown(self):
        self.shutdown_done = True


@pytest.mark.asyncio
async def test_rebuild_after_poll_stall_does_not_conflict_with_replacement_poller():
    """A rebuild after a poll stall must not raise a conflict against the replacement poller.

    This is the exact shape of the 2026-11-11 defect: the OLD Updater's shutdown-cleanup
    getUpdates fired while the NEW adapter was already polling the same bot token, and
    Telegram answered 409.
    """
    adapter = _make_adapter()
    token = _FakeBotToken()

    async def _cleanup():
        # PTB's real cleanup: a get_updates(timeout=0) on the SAME bot token.
        await token.get_updates(offset=1, timeout=0)

    updater = _FakePTBUpdater(_cleanup)
    app = _FakeApp(updater)
    adapter._app = app
    adapter._bot = MagicMock()

    # The replacement poller is already live on this token — exactly the live ordering
    # (replacement connected at 05:26:41, old cleanup fired at 05:26:44).
    token.replacement_polling = True

    await adapter.disconnect()

    assert token.cleanup_calls == 0, (
        "the discarded Updater must not issue a cleanup getUpdates: it would race the "
        "replacement poller sharing this bot token"
    )
    assert token.conflicts_raised == 0, (
        "a telegram.error.Conflict was raised against the replacement poller"
    )
    # The Updater still stopped cleanly — the fix drops the receipt, not the teardown.
    assert not updater.running
    assert app.shutdown_done


@pytest.mark.asyncio
async def test_cleanup_get_updates_is_not_issued_even_though_ptb_invokes_the_callback():
    """PTB may still invoke the (neutralised) callback — what matters is no getUpdates.

    The neutraliser replaces PTB's cleanup callback with a no-op rather than clearing it:
    clearing it makes PTB log "No polling cleanup callback defined", which reads like a
    fault to an operator. A no-op keeps the teardown path identical to PTB's own.
    """
    adapter = _make_adapter()
    token = _FakeBotToken()

    async def _cleanup():
        await token.get_updates(offset=1, timeout=0)

    updater = _FakePTBUpdater(_cleanup)
    app = _FakeApp(updater)
    adapter._app = app
    adapter._bot = MagicMock()
    token.replacement_polling = True

    await adapter.disconnect()

    assert updater.cleanup_callbacks_invoked == 1, (
        "PTB's own stop() invokes the cleanup callback when one is set; the fix neutralises "
        "its body, it does not change PTB's control flow"
    )
    assert token.cleanup_calls == 0, "the neutralised callback must not touch the bot token"


@pytest.mark.asyncio
async def test_disconnect_is_inert_when_ptb_exposes_no_cleanup_callback():
    """The neutraliser must be inert when PTB exposes no cleanup callback (renamed internals)."""
    adapter = _make_adapter()
    updater = _FakePTBUpdater(None)
    app = _FakeApp(updater)
    adapter._app = app

    assert adapter._neutralize_ptb_polling_cleanup(updater) is False
    assert not hasattr(updater, "_Updater__polling_cleanup_cb")

    await adapter.disconnect()
    assert not updater.running
    assert not adapter.has_fatal_error


@pytest.mark.asyncio
async def test_neutralizer_reports_true_only_when_a_callback_was_suppressed():
    """The helper reports whether it actually suppressed something."""
    adapter = _make_adapter()

    updater = _FakePTBUpdater(None)
    assert adapter._neutralize_ptb_polling_cleanup(updater) is False

    updater_with_cb = _FakePTBUpdater(lambda: asyncio.sleep(0))
    assert adapter._neutralize_ptb_polling_cleanup(updater_with_cb) is True
    # Suppressing is idempotent: a second call finds the (now no-op) callback and reports True.
    assert adapter._neutralize_ptb_polling_cleanup(updater_with_cb) is True

    assert adapter._neutralize_ptb_polling_cleanup(None) is False


@pytest.mark.asyncio
async def test_cleanup_suppression_preserves_the_receipt_offset_semantics():
    """Suppressing the cleanup must not stop the adapter from flushing its OWN receipts.

    The adapter's own admission-receipt flush (the dedupe that prevents a message being
    answered twice) is a separate mechanism from PTB's cleanup getUpdates. It must still run
    on disconnect — the fix only drops PTB's server-side receipt call.
    """
    adapter = _make_adapter()
    token = _FakeBotToken()

    async def _cleanup():
        await token.get_updates(offset=1, timeout=0)

    updater = _FakePTBUpdater(_cleanup)
    app = _FakeApp(updater)
    adapter._app = app

    flushed = []

    async def _fake_flush():
        flushed.append(True)

    flush_task = asyncio.ensure_future(_fake_flush())
    adapter._update_receipt_flush = flush_task

    await adapter.disconnect()

    assert flushed, "the adapter's own receipt flush must still run on disconnect"
    assert token.cleanup_calls == 0
