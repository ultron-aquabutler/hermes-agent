"""Gateway response filtering helpers.

These decide whether a completed agent turn should be delivered to the chat,
not what should be persisted in conversation history.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Exact whole-response markers meaning "the agent intentionally chose not to
# reply". Keep small and explicit; arbitrary empty output remains an
# error/empty-response path, not silence. A lane that does not think in English
# translates the sentinel rather than dropping it, and the whole control token
# then reaches the user as content, so the translated forms are carried here
# too. zh-Hans is the only non-English locale this project ships documentation
# for, which is where the list stops.
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]", "SILENT", "NO_REPLY", "NO REPLY",
    "[静默]", "静默", "[沉默]", "沉默",
})

# Bracketed markers drive the autonomous lane's prefix rule ("[SILENT] nothing
# new this tick"). Derived from the set above so a new marker cannot be added to
# one rule and forgotten in the other.
_BRACKETED_SILENCE_MARKERS = tuple(
    sorted(m for m in LIVE_GATEWAY_SILENT_MARKERS if m.startswith("["))
)

# The persisted user-row kind of a self-injected MessageEvent(internal=True) turn — the only
# machinery kind the gateway produces; only these may vanish on a bare silence marker.
INTERNAL_NOTIFICATION_DISPLAY_KIND = "internal_notification"
MACHINERY_DISPLAY_KINDS = frozenset({INTERNAL_NOTIFICATION_DISPLAY_KIND})

# Longer than any marker could plausibly be, even with stray punctuation.
_MARKER_LENGTH_CAP = 64


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _is_edge_punctuation(ch: str) -> bool:
    # Square brackets stay structural so malformed ``[SILENT`` cannot become ``SILENT``.
    return ch not in "[]" and unicodedata.category(ch).startswith("P")


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation (``.NO_REPLY``, ``*NO_REPLY*``) without erasing marker structure."""
    start, end = 0, len(text)
    while start < end and _is_edge_punctuation(text[start]):
        start += 1
    while end > start and _is_edge_punctuation(text[end - 1]):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: Any) -> tuple[str, ...]:
    """Canonical forms of a short marker-sized response; ``()`` when not a candidate at all."""
    stripped = text.strip() if isinstance(text, str) else ""
    if not 0 < len(stripped) <= _MARKER_LENGTH_CAP:
        return ()
    depunctuated = _strip_edge_silence_punctuation(stripped)
    forms = (stripped,) if depunctuated == stripped else (stripped, depunctuated)
    return tuple(_canonical_silence_candidate(f) for f in forms)


def is_intentional_silence_response(response: Any) -> bool:
    """True only when ``response`` is exactly a silence marker.

    Prose that merely mentions ``NO_REPLY`` must be delivered normally. A blank
    response is not silence either — that is the empty-response failure path.
    """
    return any(c in LIVE_GATEWAY_SILENT_MARKERS for c in _canonical_silence_candidates(response))


def is_autonomous_silence_response(response: Any) -> bool:
    """Loose silence matcher for autonomous lanes (cron, webhook).

    Models reliably bracket ``[SILENT]`` with a short note, so unlike the
    interactive EXACT rule this also suppresses when a marker sits on its own
    first/last line or the bracketed sentinel opens the response (``[SILENT] No
    changes detected``).  A token buried mid-sentence is still delivered.
    Shares :data:`LIVE_GATEWAY_SILENT_MARKERS` so the two sets cannot drift.
    """
    stripped = response.strip() if isinstance(response, str) else ""
    if not stripped:
        return False
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    # Bracketed form only for the prefix rule, so a bare "Silent retry succeeded" is NOT swallowed.
    # Same de-punctuating forms as the interactive rule, so ``【静默】`` / ``静默。`` cannot
    # be suppressed in chat yet delivered by cron.
    return stripped.upper().startswith(_BRACKETED_SILENCE_MARKERS) or any(
        is_intentional_silence_response(c) for c in (stripped, lines[0], lines[-1])
    )


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """Silence markers suppress delivery only for successful agent turns."""
    return isinstance(agent_result, dict) and not agent_result.get("failed") and is_intentional_silence_response(response)


def display_kind_for_event(event: Any) -> str | None:
    """The persisted user-row kind for a gateway turn: only self-injected events are machinery.

    A scheduled heartbeat prompt is self-injected too (``_heartbeat_session_id`` is stamped only
    by the gateway poller, never inferred from inbound text), but it deliberately stays
    non-internal so authorization and the emergency stop still apply to it.
    """
    if getattr(event, "internal", False) or getattr(event, "_heartbeat_session_id", None):
        return INTERNAL_NOTIFICATION_DISPLAY_KIND
    return None


def is_machinery_display_kind(display_kind: Any) -> bool:
    """Only a machinery turn may vanish on a bare silence marker; a human turn gets a visible fallback.

    The caller passes the current turn's persisted display kind instead of inferring it from the
    transcript: the inbound user row is not persisted yet, and a previous internal row must never
    authorize silence on a human turn.
    """
    return display_kind in MACHINERY_DISPLAY_KINDS


def is_partial_silence_marker(text: Any) -> bool:
    """True while streamed ``text`` could still resolve to a silence marker.

    A buffer whose canonical form is a non-empty *prefix* of a marker (``"NO"`` on
    the way to ``"NO_REPLY"``, or an exact marker not yet terminated by stream-end)
    is held back so a raw marker is never shown and then retracted.  Divergence
    from every marker, or exceeding the cap, resumes normal streaming.
    """
    return any(
        c and any(marker.startswith(c) for marker in LIVE_GATEWAY_SILENT_MARKERS)
        for c in _canonical_silence_candidates(text)
    )


# The interrupt checkpoint scaffold the agent loop builds when a mid-flight user correction
# lands (``agent/conversation_loop`` — marker + two headers). It is REPLAY text for the next
# provider request: the user row carries the human's words in ``content`` and the scaffold only
# in ``api_content``. A model holding the scaffold in context can reproduce it as its own
# assistant text (#81841), and that echo reaches the chat verbatim — a phantom
# "[This response was interrupted by a user correction.]" reporting an interruption that never
# happened and carrying no user payload. Peers read it as a real turn and re-answer it, so one
# echo becomes a loop.
#
# Only the scaffold's own casing matches, and the block must open with a BRACKETED marker or
# context header: a sentence that merely says the response was interrupted is prose and must be
# delivered. The optional ``]``/``.`` cover a streamed echo seen before the marker finished; each
# header is optional so a partially-reached checkpoint (marker only, or marker + one header) is
# still caught. Copies of these literals live in ``agent/conversation_loop`` and are kept in
# lock-step by test_interrupt_scaffold_echo.py — importing the agent package here would drag the
# whole loop into every gateway import.
_INTERRUPT_SCAFFOLD_MARKER = "[This response was interrupted by a user correction.]"
_INTERRUPT_SCAFFOLD_CONTEXT_HEADER = "[Context from the interrupted assistant response]"
_INTERRUPT_SCAFFOLD_VISIBLE_HEADER = "Visible response before the interruption:"

_SCAFFOLD_CONTEXT_RE = re.compile(
    r"\[Context from the interrupted assistant response\.?]?"
)
_SCAFFOLD_VISIBLE_RE = re.compile(r"Visible response before the interruption:?")
_SCAFFOLD_MARKER_RE = re.compile(
    r"\[This response was interrupted by a user correction\.?]?"
)
_SCAFFOLD_BLANK_RE = re.compile(r"[ \t\r\n]+")


def _scaffold_prefix_end(text: str) -> int:
    """Length of the leading interrupt-checkpoint block in ``text`` (0 when it has none).

    Walks the scaffold atoms (marker, either header) in any order, then requires the marker
    to have been seen: a stray header with no marker is not scaffolding, it is broken output
    the gateway should leave alone rather than silently eat.
    """
    blank = _SCAFFOLD_BLANK_RE.match(text)
    pos = blank.end() if blank else 0
    saw_marker = False
    while True:
        rest = text[pos:]
        for atom, is_marker in (
            (_SCAFFOLD_MARKER_RE, True),
            (_SCAFFOLD_CONTEXT_RE, False),
            (_SCAFFOLD_VISIBLE_RE, False),
        ):
            match = atom.match(rest)
            if match is None:
                continue
            pos += match.end()
            saw_marker = saw_marker or is_marker
            break
        else:
            break
        # Blank lines separate the scaffold's own paragraphs: they stay inside the block, and
        # the next pass decides whether another atom (or the payload) follows.
        blank = _SCAFFOLD_BLANK_RE.match(text[pos:])
        if blank:
            pos += blank.end()
    return pos if saw_marker else 0


def strip_interrupt_scaffold(response: Any) -> str:
    """Drop a leading interrupt-checkpoint block from ``response``.

    Returns ``response`` unchanged unless it OPENS with the scaffold, so prose that merely
    mentions an interruption survives; returns ``""`` when the response was nothing but
    scaffolding (see :func:`is_interrupt_scaffold_echo`). Text after the dropped block is real
    assistant output and is returned as the delivered body.
    """
    if not isinstance(response, str):
        return response
    end = _scaffold_prefix_end(response)
    return response[end:].lstrip() if end else response


def is_interrupt_scaffold_echo(response: Any) -> bool:
    """True when ``response`` is only a leaked interrupt scaffold — no assistant payload.

    This is the phantom-interruption stub: deliverable text that exists because the scaffold
    leaked, not because the agent had anything to say. An echo that DOES carry payload after
    the scaffold is not a stub and must still be delivered — use
    :func:`strip_interrupt_scaffold` to drop the misleading header from it instead.
    """
    if not isinstance(response, str) or not response.strip():
        return False
    return not strip_interrupt_scaffold(response).strip()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

SILENT_REPLY_TOKEN = "NO_REPLY"
# ---- END PLUGIN-COMPAT ----
