"""Phase 3/3 verification for t_045c7cee — interrupt-recursion interaction and
escalation-path survival, tested against the LIVE deployed adapter.

STEP 3 — INTERRUPT-RECURSION INTERACTION
  A cron ping delivered to a group topic must not create an agent turn. A turn
  is what feeds the interrupt-recursion guard:
      "Interrupt recursion depth N reached for session ... -- queueing message
       instead of recursing."
  The guard is per-session: while the hub is answering a ping on
  agent:main:telegram:group:-1004452024623:<topic>, the NEXT inbound on that
  session recurses. The recursion guard then eats a REAL message (Bryan's DM)
  instead of the noise. So the ping must never become a turn.

STEP 4 — ESCALATION PATH MUST SURVIVE
  (a) blocked-card escalation "@vision_wallace_bot I need help with **{title}**
      -- {reason}" from a HUMAN   -> dispatched
  (b) the same escalation from a BOT account -> dispatched (the explicit
      @mention satisfies the bot-to-bot veto)
  (c) Bryan's DM                                  -> dispatched
  (d) a human quoting the cron envelope            -> dispatched (a real request)
  (e) an escalation addressed to ANOTHER bot        -> NOT dispatched by the hub
  (f) a cron envelope from a bot                   -> NOT dispatched (the echo is gone)

Run:  cd /home/serveradmin/.hermes/hermes-agent && \
      HERMES_HOME=/tmp/hermes-phase3-home-$$ \
      timeout 500 scripts/run_tests.sh tests/gateway/test_phase3_cron_echo_recursion.py -q
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------- constants

WA_SERVICES_GROUP = -1004452024623
HUB_BOT_ID = 999
HUB_BOT_USERNAME = "vision_wallace_bot"
ULTRON_BOT_ID = 555
BRYAN_USER_ID = 8943042074

# The exact envelope the delivery layer writes (cron/scheduler_delivery.py),
# flattened onto one line by the Telegram delivery path.
CRON_ENVELOPE = (
    "Cronjob Response: mac-ssh-key-watchdog (job_id: 4155843881c1) ------------ "
    "OK ssh key fingerprint unchanged\n\n"
    "To stop or manage this job, send me a new message (e.g. \"stop reminder mac-ssh-key-watchdog\")."
)

BLOCKED_CARD_ESCALATION = (
    "@vision_wallace_bot I need help with **MC compose deploy-verify tooling** -- "
    "needs attention: worker exhausted its iteration budget"
)

OTHER_BOT_ESCALATION = (
    "@ultron_wallace_bot I need help with **Patroni disk usage** -- needs attention"
)


@pytest.fixture(autouse=True)
def _restore_telegram_env():
    saved = {k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")}
    yield
    for k in [k for k in os.environ if k.startswith("TELEGRAM_")]:
        if k not in saved:
            del os.environ[k]
    os.environ.update(saved)


def _make_adapter(**extra_overrides):
    """Adapter configured EXACTLY like the deployed hub profile.

    Live config (profiles/default + ~/.hermes/config.yaml):
        exclusive_bot_mentions: true
        bots_require_mention: true
        require_mention: true
        free_response_chats: 8943042074      (Bryan's DM only)
        allowed_chats: -1004452024623,-1004486548929,-5499917102
    The WA Services group is NOT a free-response scope for the hub.
    """
    from plugins.platforms.telegram.adapter import TelegramAdapter

    extra = {
        "require_mention": True,
        "bots_require_mention": True,
        "exclusive_bot_mentions": True,
        "free_response_chats": "8943042074",
        "free_response_topics": "",
        "mention_patterns": "",
        "allowed_chats": [str(WA_SERVICES_GROUP)],
        "group_allowed_chats": [str(WA_SERVICES_GROUP)],
        "allowed_topics": [],
        "guest_mode": False,
        "observe_unmentioned_group_messages": False,
    }
    extra.update(extra_overrides)

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=HUB_BOT_ID, username=HUB_BOT_USERNAME)
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = adapter._compile_mention_patterns()
    adapter._forum_lock = asyncio.Lock()
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._is_callback_user_authorized = lambda user_id, **_kw: True
    return adapter


def _bot_group_message(text, *, chat_id=WA_SERVICES_GROUP, thread_id=3560,
                       from_user_id=ULTRON_BOT_ID, from_user_name="Ultron",
                       is_bot=True, entities=None):
    return SimpleNamespace(
        message_id=4242,
        text=text,
        caption=None,
        entities=entities or [],
        caption_entities=[],
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
        chat=SimpleNamespace(id=chat_id, type="group", title="WallaceArizona Services", is_forum=True),
        from_user=SimpleNamespace(id=from_user_id, full_name=from_user_name,
                                  first_name=from_user_name, is_bot=is_bot),
        reply_to_message=None,
        date=None,
    )


def _human_group_message(text, *, chat_id=WA_SERVICES_GROUP, thread_id=3560,
                         from_user_id=BRYAN_USER_ID, from_user_name="Bryan Wallace",
                         entities=None):
    return _bot_group_message(text, chat_id=chat_id, thread_id=thread_id,
                              from_user_id=from_user_id, from_user_name=from_user_name,
                              is_bot=False, entities=entities)


def _dm_message(text, *, from_user_id=BRYAN_USER_ID, from_user_name="Bryan Wallace"):
    return SimpleNamespace(
        message_id=4343,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        chat=SimpleNamespace(id=from_user_id, type="private", full_name=from_user_name,
                             title=None, is_forum=False),
        from_user=SimpleNamespace(id=from_user_id, full_name=from_user_name,
                                  first_name="Bryan", is_bot=False),
        reply_to_message=None,
        date=None,
    )


def _mention_entities(text, mentions):
    out = []
    for m in mentions:
        offset = text.index(m)
        out.append(SimpleNamespace(type="mention", offset=offset, length=len(m)))
    return out


def _update(msg):
    return SimpleNamespace(update_id=9001, message=msg, effective_message=None)


async def _flush_text_batches(adapter):
    """Let the adapter's text-batch timer fire so a dispatched message reaches
    ``_message_handler``. The adapter buffers a text chunk and flushes it on a
    short timer; without this the assertion races the flush.
    """
    for _ in range(50):
        if adapter._message_handler.await_count:
            return
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------- STEP 3

def test_step3_cron_ping_creates_no_agent_turn():
    """STEP 3: a cron ping in a group topic must NOT be dispatched.

    A dispatched ping becomes a turn; a turn in flight makes the NEXT inbound on
    that session recurse, which is what trips
    "Interrupt recursion depth N reached for session ... -- queueing message
    instead of recursing." Dropping the ping at the adapter removes the load
    that feeds that recursion.
    """
    async def _run():
        adapter = _make_adapter()
        msg = _bot_group_message(CRON_ENVELOPE)

        assert adapter._should_process_message(msg) is False, (
            "the cron envelope passed the trigger gate -- it would create a turn "
            "and feed the interrupt-recursion guard"
        )
        assert adapter._bot_sender_suppressed(msg) is True, (
            "the bot-to-bot veto did not fire for the cron envelope"
        )

        await adapter._handle_text_message(_update(msg), SimpleNamespace())
        await _flush_text_batches(adapter)

        assert adapter._message_handler.await_count == 0, (
            "the cron ping was dispatched -- it created a turn"
        )
        assert adapter._pending_messages == {}, "the cron ping queued a pending message"
        assert adapter._pending_text_batches == {}, "the cron ping created a text batch"

    asyncio.run(_run())


def test_step3_cron_ping_never_reaches_the_busy_or_cold_path():
    """STEP 3: the ping is dropped UPSTREAM of both the busy and the cold path.

    The adapter's trigger gate (``_should_process_message``) runs inside
    ``_handle_text_message``, BEFORE the MessageEvent is built. Only an event
    that passes that gate is enqueued and later handed to the runner's
    ``handle_message`` -> ``_busy_session_handler`` (gateway/platforms/base.py
    line ~4029). So a cron ping never becomes a queued follow-up behind a
    running turn: the recursion load is removed at the source, not merely
    suppressed at the send.

    Assert the gate on both paths:
      * the adapter gate refuses the ping, so no event is built or enqueued;
      * a busy session handler registered on the adapter is never invoked.
    """
    async def _run():
        adapter = _make_adapter()
        busy_calls = []

        async def _busy_handler(event, session_key):
            busy_calls.append((event, session_key))
            return True

        adapter.set_busy_session_handler(_busy_handler)
        msg = _bot_group_message(CRON_ENVELOPE)

        assert adapter._should_process_message(msg) is False, (
            "the cron envelope passed the trigger gate -- it would create a turn "
            "and feed the interrupt-recursion guard"
        )

        await adapter._handle_text_message(_update(msg), SimpleNamespace())
        await _flush_text_batches(adapter)

        assert busy_calls == [], (
            "the cron ping reached the busy-session handler -- it would be queued "
            "behind the running turn and drive the recursion"
        )
        assert adapter._message_handler.await_count == 0, "the cron ping created a turn"
        assert adapter._pending_messages == {}, "the cron ping was queued as a pending message"
        assert adapter._pending_text_batches == {}, "the cron ping created a text batch"

    asyncio.run(_run())


def test_step3_cron_ping_in_every_delivery_topic_is_dropped():
    """STEP 3: every topic a cron job actually delivers to must be covered.

    The delivery targets come from the live ultron cron job list:
    5620, 26613, 3215, 8190, 3560, 3199. The gate is chat-scoped, so all of
    them must be dropped — the fix is not topic-specific.
    """
    async def _run():
        adapter = _make_adapter()
        for topic in (3560, 5620, 26613, 3215, 8190, 3199):
            msg = _bot_group_message(CRON_ENVELOPE, thread_id=topic)
            assert adapter._should_process_message(msg) is False, (
                f"cron envelope was NOT dropped in topic {topic}"
            )

    asyncio.run(_run())


# ---------------------------------------------------------------- STEP 4

def test_step4_blocked_card_escalation_from_a_human_still_reaches_the_agent():
    """STEP 4(a): the blocked-card escalation format must survive.

    "@vision_wallace_bot I need help with **{title}** -- {reason}" is posted by
    the kanban watcher (scripts/kanban_board_watcher.py). It carries an
    EXPLICIT mention of the hub bot, so the bot-to-bot veto does not apply and
    the agent must see it.
    """
    async def _run():
        adapter = _make_adapter()
        msg = _human_group_message(
            BLOCKED_CARD_ESCALATION,
            entities=_mention_entities(BLOCKED_CARD_ESCALATION, ["@vision_wallace_bot"]),
        )

        assert adapter._should_process_message(msg) is True, (
            "the blocked-card escalation was refused -- the escalation path is broken"
        )

        await adapter._handle_text_message(_update(msg), SimpleNamespace())
        await _flush_text_batches(adapter)

        assert adapter._message_handler.await_count == 1, (
            "the blocked-card escalation never reached the agent"
        )

    asyncio.run(_run())


def test_step4_escalation_from_a_bot_sender_still_reaches_the_agent():
    """STEP 4(b): a watcher that posts as a BOT account still gets through.

    The bot-to-bot veto requires an explicit @mention. The escalation carries
    the hub's handle in the text, so the mention satisfies the veto.
    """
    async def _run():
        adapter = _make_adapter()
        msg = _bot_group_message(
            BLOCKED_CARD_ESCALATION,
            entities=_mention_entities(BLOCKED_CARD_ESCALATION, ["@vision_wallace_bot"]),
        )

        assert adapter._bot_sender_suppressed(msg) is False, (
            "the bot-to-bot veto fired even though the escalation @mentions the hub"
        )
        assert adapter._should_process_message(msg) is True, (
            "a bot-posted escalation carrying an explicit @mention was refused"
        )

        await adapter._handle_text_message(_update(msg), SimpleNamespace())
        await _flush_text_batches(adapter)

        assert adapter._message_handler.await_count == 1, (
            "the bot-posted escalation never reached the agent"
        )

    asyncio.run(_run())


def test_step4_bryan_dm_still_reaches_the_agent():
    """STEP 4(c): Bryan's direct messages must still get through."""
    async def _run():
        adapter = _make_adapter()
        msg = _dm_message("Vision look at your responses on the WallaceArizona Services group.")

        assert adapter._should_process_message(msg) is True, "Bryan's DM was refused by the trigger gate"

        await adapter._handle_text_message(_update(msg), SimpleNamespace())
        await _flush_text_batches(adapter)

        assert adapter._message_handler.await_count == 1, "Bryan's DM never reached the agent"

    asyncio.run(_run())


def test_step4_bryan_dm_quoting_a_cron_post_still_reaches_the_agent():
    """STEP 4(d): a HUMAN quoting the cron envelope is not a machine ping.

    The envelope is only recognised at the START of a BOT's message. Bryan
    pasting the envelope to ask about it is a real request and must be answered.
    """
    async def _run():
        adapter = _make_adapter()
        msg = _dm_message("What is this? " + CRON_ENVELOPE)

        await adapter._handle_text_message(_update(msg), SimpleNamespace())
        await _flush_text_batches(adapter)

        assert adapter._message_handler.await_count == 1, (
            "a human quoting the cron envelope was silenced -- that is a real request"
        )

    asyncio.run(_run())


def test_step4_escalation_addressed_to_another_bot_is_not_hijacked():
    """STEP 4(e): an escalation addressed to ANOTHER bot must not wake the hub.

    The other half of "must survive": silencing genuine escalations is bad, but
    so is answering someone else's escalation. The exclusive-mention gate owns
    that, and the cron-echo fix must not weaken it.
    """
    async def _run():
        adapter = _make_adapter()
        msg = _human_group_message(
            OTHER_BOT_ESCALATION,
            entities=_mention_entities(OTHER_BOT_ESCALATION, ["@ultron_wallace_bot"]),
        )

        assert adapter._should_process_message(msg) is False, (
            "the hub answered an escalation addressed to a different bot"
        )

        await adapter._handle_text_message(_update(msg), SimpleNamespace())
        await _flush_text_batches(adapter)

        assert adapter._message_handler.await_count == 0, (
            "the hub answered an escalation addressed to a different bot"
        )

    asyncio.run(_run())


def test_step4_a_cron_envelope_that_carries_an_escalation_is_not_silenced_by_the_bot_veto():
    """STEP 4(f): a cron payload carrying an escalation must not be dropped.

    The envelope is a machine ping; the escalation inside it is a real request.
    The bot-to-bot veto is a MENTION gate, so an escalation that @mentions the
    hub survives it — the cron-echo work must not add a second, blunter gate
    that would silence it.
    """
    async def _run():
        adapter = _make_adapter()
        envelope_with_escalation = (
            "Cronjob Response: patroni-disk-usage-watch (job_id: 4155843881c1) ------------ "
            "@vision_wallace_bot I need help with **Patroni disk usage** -- needs attention"
        )
        msg = _bot_group_message(
            envelope_with_escalation,
            entities=_mention_entities(envelope_with_escalation, ["@vision_wallace_bot"]),
        )

        assert adapter._bot_sender_suppressed(msg) is False, (
            "the escalation inside a cron envelope was vetoed by the bot-to-bot gate"
        )
        assert adapter._should_process_message(msg) is True, (
            "the escalation inside a cron envelope was refused"
        )

    asyncio.run(_run())
