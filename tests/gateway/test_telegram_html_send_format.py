"""Regression tests for Telegram HTML-vs-Markdown send-format detection.

Background
----------
On 2026-10-29 a message posted to Bryan's Telegram DM arrived with literal
``<b>`` tags visible in the text. The gateway reply path hard-coded
``ParseMode.MARKDOWN_V2`` with no HTML detection. Telegram's MarkdownV2
reserved set contains ``>`` but NOT ``<``, so the MarkdownV2 escaper escaped
the ``>`` of ``</b>`` and left the ``<`` alone::

    input:    <b>Bold</b>
    escaped:  <b\\>Bold</b\\>

Telegram accepts that with HTTP 200 — it is NOT a parse error — and renders the
tags literally. Nothing failed, so nothing was logged.

These tests assert on the *parse mode chosen* and on the *absence of
``<b\\>``-style escapes*, not on a rendered screenshot.

The detection routine is shared with the ``_send_telegram`` tool
(``tools/send_message_senders.py::_telegram_format``) so the two paths cannot
diverge again.
"""

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.telegram_format import (
    escape_html_text,
    has_html_tags,
    strip_html_tags,
)

try:
    from telegram.constants import ParseMode
except Exception:  # pragma: no cover - telegram package may be mocked out
    ParseMode = None


@pytest.fixture()
def adapter():
    """An adapter with no live bot — ``format_for_send`` is pure."""
    return TelegramAdapter.__new__(TelegramAdapter)


# =========================================================================
# The original bug: '<b>' in a reply rendered as literal raw tags
# =========================================================================


class TestGatewayReplyHtmlDetection:
    def test_bold_tag_uses_html_parse_mode(self, adapter):
        """A reply containing '<b>' must be sent as HTML, not MarkdownV2."""
        text, mode = adapter.format_for_send("<b>Bold</b>")
        assert mode == ParseMode.HTML
        assert text == "<b>Bold</b>"

    def test_no_markdownv2_escapes_in_html_mode(self, adapter):
        """The regression itself: the buggy output was '<b\\\\>Bold</b\\\\>'.

        That escaped '>' rendered as a visible '>' next to a literal '<',
        producing '<b>Bold</b>' as plain text in the chat.
        """
        text, mode = adapter.format_for_send("<b>Bold</b>")
        assert mode == ParseMode.HTML
        assert "<b\\>" not in text
        assert "</b\\>" not in text
        assert "\\>" not in text
        # The tags themselves are intact, so Telegram renders real bold.
        assert "<b>" in text and "</b>" in text

    @pytest.mark.parametrize("snippet,tag", [
        ("<b>bold</b>", "b"),
        ("<i>italic</i>", "i"),
        ("<code>x</code>", "code"),
        ("<pre>block</pre>", "pre"),
    ])
    def test_each_supported_tag_uses_html_mode(self, adapter, snippet, tag):
        text, mode = adapter.format_for_send(snippet)
        assert mode == ParseMode.HTML
        assert f"<{tag}>" in text
        assert f"</{tag}>" in text

    def test_plain_markdown_still_uses_markdownv2(self, adapter):
        """Markdown replies keep the MarkdownV2 path — no behaviour change."""
        text, mode = adapter.format_for_send("**Bold** and `code`")
        assert mode == ParseMode.MARKDOWN_V2
        assert "<b>" not in text

    def test_empty_content(self, adapter):
        text, mode = adapter.format_for_send("")
        assert text == ""
        assert mode == ParseMode.MARKDOWN_V2


# =========================================================================
# Mixed content: a literal '<' that is NOT a tag must survive intact
# =========================================================================


class TestMixedContentSafety:
    @pytest.mark.parametrize("prose", [
        "a < b",
        "x <y> z",
        "5<10 and 20>15",
        "count < max",
    ])
    def test_prose_less_than_is_not_detected_as_html(self, adapter, prose):
        """A reply with prose '<' must NOT be switched to HTML mode.

        Switching it would break it: Telegram's HTML parser treats '<y>' as an
        unsupported tag and rejects the whole message with HTTP 400.
        """
        text, mode = adapter.format_for_send(prose)
        assert mode == ParseMode.MARKDOWN_V2, f"{prose!r} was misdetected as HTML"

    def test_real_tag_plus_prose_less_than(self, adapter):
        """Real tags AND prose '<' in the same message: HTML mode, prose escaped.

        The stray '<' must be escaped so Telegram's HTML parser cannot mistake
        it for markup — otherwise the message is dropped or rejected.
        """
        text, mode = adapter.format_for_send("<b>result</b> and a < b")
        assert mode == ParseMode.HTML
        assert "<b>result</b>" in text
        assert "a &lt; b" in text
        assert "a < b" not in text  # the raw '<' must not survive into HTML mode

    def test_real_tag_plus_angle_bracket_pair(self, adapter):
        text, mode = adapter.format_for_send("<b>result</b> and x <y> z")
        assert mode == ParseMode.HTML
        assert "<b>result</b>" in text
        assert "&lt;y&gt;" in text
        assert "<y>" not in text


# =========================================================================
# The shared detection helper
# =========================================================================


class TestHasHtmlTags:
    @pytest.mark.parametrize("text", [
        "<b>bold</b>",
        "<i>italic</i>",
        "<code>x</code>",
        "<pre>block</pre>",
        "<a href='http://example.com'>link</a>",
        "<tg-spoiler>hidden</tg-spoiler>",
        "<blockquote>quoted</blockquote>",
        "<b>a</b> and <i>b</i>",
    ])
    def test_detects_real_tags(self, text):
        assert has_html_tags(text) is True

    @pytest.mark.parametrize("text", [
        "a < b",
        "x <y> z",
        "5<10 and 20>15",
        "<!-- an html comment -->",
        "<notasupportedtag>",
        "a <b and b> c",
        "<b>unclosed",
        "</b>close without open",
        "",
        "no angle brackets at all",
    ])
    def test_rejects_non_html(self, text):
        assert has_html_tags(text) is False

    def test_crossed_nesting_is_rejected(self):
        """Crossed tags are not well-formed — Telegram would reject them."""
        assert has_html_tags("<b>bold <i>both</b></i>") is False


class TestEscapeHtmlText:
    def test_preserves_known_tags(self):
        assert escape_html_text("<b>x</b>") == "<b>x</b>"

    def test_escapes_stray_less_than(self):
        assert escape_html_text("a < b") == "a &lt; b"

    def test_escapes_ampersand(self):
        assert escape_html_text("tom & jerry") == "tom &amp; jerry"

    def test_round_trip_strip(self):
        """strip_html_tags undoes escape_html_text for the plain-text fallback."""
        assert strip_html_tags(escape_html_text("<b>x</b> & y")) == "x & y"


# =========================================================================
# The two send paths must agree (one shared helper, not two copies)
# =========================================================================


class TestSendPathsAgree:
    def test_tool_path_uses_the_same_detection(self):
        """``tools.send_message_senders._telegram_format`` and the reply path
        must make the SAME HTML/Markdown decision for the same input."""
        from tools.send_message_senders import _telegram_format

        for text in ("<b>bold</b>", "a < b", "x <y> z", "**markdown**"):
            _tool_text, tool_mode, tool_has_html = _telegram_format(text)
            adapter = TelegramAdapter.__new__(TelegramAdapter)
            _reply_text, reply_mode = adapter.format_for_send(text)
            assert tool_has_html == (reply_mode == ParseMode.HTML), (
                f"paths disagree on {text!r}: tool has_html={tool_has_html}, "
                f"reply mode={reply_mode}")
