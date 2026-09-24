"""Leaked interrupt-scaffold echo suppression (phantom interruptions).

The agent loop writes an interrupt CHECKPOINT into the next provider request when a
mid-flight user correction lands (``agent/conversation_loop._INTERRUPT_SCAFFOLD_MARKER``
plus its two headers).  It is replay text: the user row keeps the human's own words in
``content`` and carries the scaffold only in ``api_content``.  A model holding that
scaffold in context can reproduce it as its own assistant text (#81841), and then the
gateway delivered it verbatim — a phantom

    [This response was interrupted by a user correction.]

message in the chat, reporting an interruption no user ever caused, carrying no payload.
Peer agents on a shared channel read it as a real turn and answered it, so one echo became
a loop (observed: a crash-looping worker, 54 crashes in 90 min, with the hub and the peers
amplifying each stub).

These tests pin the three halves of the fix:

* ``is_interrupt_scaffold_echo`` / ``strip_interrupt_scaffold`` — the delivery predicate.
* ``GatewayStreamConsumer`` — a scaffold-only stream is retracted like a silence marker
  (the streamed preview IS the delivered message, so the whole-response filter is too late).
* ``GatewayTurnMixin._scrub_interrupt_scaffold`` — the non-streaming delivery path.

The acceptance property (a crash loop emits ~zero stubs) is asserted end-to-end in
``test_crash_loop_emits_no_scaffold_stub``: it drives many simulated crash-loop turns and
counts what would reach the chat.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.response_filters import (
    _INTERRUPT_SCAFFOLD_CONTEXT_HEADER,
    _INTERRUPT_SCAFFOLD_MARKER,
    _INTERRUPT_SCAFFOLD_VISIBLE_HEADER,
    is_interrupt_scaffold_echo,
    strip_interrupt_scaffold,
)
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

MARKER = _INTERRUPT_SCAFFOLD_MARKER
CONTEXT_HEADER = _INTERRUPT_SCAFFOLD_CONTEXT_HEADER
VISIBLE_HEADER = _INTERRUPT_SCAFFOLD_VISIBLE_HEADER

# Every shape seen in the live gateway logs (2026-08-07/08-08 Ultron, 2026-09-02 Vision) plus
# the truncated/partial forms a stream can expose.
STUB_SHAPES = [
    MARKER,
    f"  \n\n{MARKER}\n",
    f"{MARKER}\n\n{VISIBLE_HEADER}\n",
    f"{CONTEXT_HEADER}\n{MARKER}\n\n{VISIBLE_HEADER}\n",
    f"{MARKER} {VISIBLE_HEADER} ",                       # newline-collapsed by the transport
    f"{MARKER} {VISIBLE_HEADER} ",                       # same, trailing space
    MARKER.rstrip("]"),                                  # streamed before the marker finished
    f"{CONTEXT_HEADER}\n\n{VISIBLE_HEADER}\n\n{MARKER}",  # headers ahead of the marker
]

# Prose that mentions an interruption is NOT scaffolding and must be delivered.
DELIVERED_SHAPES = [
    "This response was interrupted by a user correction.\n\nSo I retried and it worked.",
    "The response was interrupted by a user correction — here is what I had.",
    f"Status: {MARKER} is replay text.",
    f"See {MARKER} in the transcript.",
    "",
    "   ",
    f"{VISIBLE_HEADER}\nnothing else",
    None,
    "",
]

# Scaffold echo that carries real payload: the header goes, the payload stays.
PAYLOAD_SHAPES = [
    (f"{MARKER}\n\nHere is the half-finished answer.", "Here is the half-finished answer."),
    (f"{MARKER}\n\n{VISIBLE_HEADER}\nThe digest:\n- one\n- two", "The digest:\n- one\n- two"),
    (f"{CONTEXT_HEADER}\n{MARKER}\n\npartial prose", "partial prose"),
    (f"{MARKER} tail prose", "tail prose"),
]


# --------------------------------------------------------------------------
# Predicate
# --------------------------------------------------------------------------

def test_scaffold_literals_match_the_agent_loop():
    """The gateway copy of the scaffold must not drift from the loop that writes it.

    ``gateway.response_filters`` keeps its own literals (importing ``agent.conversation_loop``
    would drag the whole turn loop into every gateway/stream import), so this is the lock-step
    check that makes the copy safe.
    """
    from agent import conversation_loop

    assert conversation_loop._INTERRUPT_SCAFFOLD_MARKER == _INTERRUPT_SCAFFOLD_MARKER


@pytest.mark.parametrize("text", STUB_SHAPES)
def test_scaffold_only_replies_are_stubs(text):
    assert is_interrupt_scaffold_echo(text) is True
    assert strip_interrupt_scaffold(text).strip() == ""


@pytest.mark.parametrize("text", DELIVERED_SHAPES)
def test_prose_mentioning_an_interruption_is_delivered(text):
    assert is_interrupt_scaffold_echo(text) is False
    assert strip_interrupt_scaffold(text) == text or text is None


@pytest.mark.parametrize("text,payload", PAYLOAD_SHAPES)
def test_payload_after_the_scaffold_survives(text, payload):
    assert is_interrupt_scaffold_echo(text) is False
    assert strip_interrupt_scaffold(text) == payload


def test_strip_is_idempotent():
    """Callers on several paths scrub the same bytes; a second pass must be a no-op."""
    for text, payload in PAYLOAD_SHAPES:
        once = strip_interrupt_scaffold(text)
        assert strip_interrupt_scaffold(once) == once == payload


# --------------------------------------------------------------------------
# GatewayTurnMixin._scrub_interrupt_scaffold — non-streaming delivery path
# --------------------------------------------------------------------------

def _turn_mixin():
    from gateway.run_turn import GatewayTurnMixin

    return GatewayTurnMixin.__new__(GatewayTurnMixin)


@pytest.mark.parametrize("text", STUB_SHAPES)
def test_shape_helper_reports_a_stub(text):
    cleaned, is_stub = _turn_mixin()._scrub_interrupt_scaffold(text, "sess")
    assert (cleaned, is_stub) == ("", True)


@pytest.mark.parametrize("text", DELIVERED_SHAPES)
def test_shape_helper_leaves_real_replies_alone(text):
    cleaned, is_stub = _turn_mixin()._scrub_interrupt_scaffold(text, "sess")
    assert cleaned == text and is_stub is False


def test_shape_helper_keeps_the_payload_of_an_echo():
    cleaned, is_stub = _turn_mixin()._scrub_interrupt_scaffold(
        f"{MARKER}\n\n{VISIBLE_HEADER}\nreal answer", "sess",
    )
    assert cleaned == "real answer" and is_stub is False


# --------------------------------------------------------------------------
# GatewayStreamConsumer — the streamed path IS the delivered message
# --------------------------------------------------------------------------

def _make_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview_1"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="preview_1"))
    adapter.delete_message = AsyncMock(return_value=True)
    return adapter


def _visible_texts(adapter):
    texts = [call.kwargs.get("content", "") for call in adapter.send.call_args_list]
    for call in adapter.edit_message.call_args_list:
        texts.append(call.kwargs.get("content", ""))
    return texts


async def _run_stream(adapter, text, *, primed_preview: bool = False) -> GatewayStreamConsumer:
    consumer = GatewayStreamConsumer(
        adapter, "chat_1", StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
    )
    if primed_preview:
        # Simulate the pre-fix behaviour: a preview of the echo was already on screen.
        consumer._message_id = "preview_1"
        consumer._preview_message_ids = {"preview_1"}
        consumer._already_sent = True
    consumer.on_delta(text)
    consumer.finish()
    await consumer.run()
    return consumer


class TestStreamedScaffoldSuppression:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", STUB_SHAPES)
    async def test_scaffold_only_stream_reaches_no_chat(self, text):
        adapter = _make_adapter()
        consumer = await _run_stream(adapter, text)

        for delivered in _visible_texts(adapter):
            assert MARKER not in delivered, f"phantom interruption leaked: {delivered!r}"
            assert VISIBLE_HEADER not in delivered, f"scaffold leaked: {delivered!r}"

        # Delivery flags stay False, so the gateway's own filter still owns what goes out next.
        assert consumer.final_response_sent is False
        assert consumer.final_content_delivered is False
        assert consumer.already_sent is False

    @pytest.mark.asyncio
    async def test_streamed_echo_preview_is_retracted(self):
        adapter = _make_adapter()
        await _run_stream(adapter, MARKER, primed_preview=True)
        adapter.delete_message.assert_awaited_once_with("chat_1", "preview_1")

    @pytest.mark.asyncio
    async def test_streamed_echo_with_payload_delivers_only_the_payload(self):
        adapter = _make_adapter()
        consumer = await _run_stream(adapter, f"{MARKER}\n\n{VISIBLE_HEADER}\nThe real answer.")

        delivered = "\n".join(t for t in _visible_texts(adapter) if t)
        assert "The real answer." in delivered
        assert MARKER not in delivered
        assert VISIBLE_HEADER not in delivered
        # Recorded payload matches what the chat saw, so the gateway does not re-send the echo.
        assert consumer.delivered_final_matches("The real answer.") is True


# --------------------------------------------------------------------------
# Acceptance property: a crash loop emits ~zero stubs
# --------------------------------------------------------------------------

def test_crash_loop_emits_no_scaffold_stub():
    """The card's acceptance criterion, as an observable property.

    A crash-looping worker ends every attempt with the same scaffold-only turn. Before the
    fix each attempt put one phantom-interruption message into the chat (plus whatever the
    hub and the peers amplified). Run 54 simulated turns — the live crash loop's count — and
    count the deliveries that contain the scaffold.
    """
    runner = _turn_mixin()
    crash_turns = [
        {"failed": True, "interrupted": True, "api_calls": 0,
         "final_response": f"{MARKER}\n\n{VISIBLE_HEADER}"},
        {"failed": True, "interrupted": True, "api_calls": 0, "final_response": MARKER},
        {"interrupted": True, "api_calls": 3, "final_response": f"{CONTEXT_HEADER}\n{MARKER}"},
    ]

    delivered = []
    for attempt in range(54):
        result = crash_turns[attempt % len(crash_turns)]
        response, is_stub = runner._scrub_interrupt_scaffold(result["final_response"], f"t_8f317627:{attempt}")
        # A stub never reaches the wire; a failed turn leaves "" for the failure-copy path.
        if not is_stub:
            delivered.append(response)

    assert delivered == [], f"crash loop still emitted {len(delivered)} scaffold deliveries"
    assert all(MARKER not in text for text in delivered)