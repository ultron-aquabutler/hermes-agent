"""Shared Telegram send-format detection: does this text want HTML or Markdown?

WHY THIS MODULE EXISTS
-----------------------
Three send paths reach Telegram, and they do NOT accept the same input:

| Path | How it is sent | What it accepts |
|---|---|---|
| gateway reply (agent answer in a chat) | ``format_message()`` -> MarkdownV2 | Markdown |
| ``_send_telegram`` tool | sniffs for tags -> HTML, else MarkdownV2 | either |
| ``tg_post.sh`` / ``md2html.py`` | source markdown -> HTML | Markdown |

The gateway reply path used to hard-code ``ParseMode.MARKDOWN_V2`` with no HTML
detection. Because Telegram's MarkdownV2 reserved set contains ``>`` but NOT
``<``, the MarkdownV2 escaper escaped the ``>`` of a hand-written ``<b>`` and
left the ``<`` alone::

    input:    <b>Bold</b>
    escaped:  <b\\>Bold</b\\>

Telegram accepts that with HTTP 200 (it is not a parse error) and renders
``<b>Bold</b>`` as PLAIN TEXT — the user sees the tags. Observed 2026-10-29:
a 2.5 KB decision summary reached Bryan with every heading wrapped in visible
``<b>`` tags.

This module holds the ONE detection routine used by every send path, so the
paths cannot drift apart again.

THE DETECTION RULE
------------------
A text is treated as HTML only when it contains **well-formed, balanced tags
whose names Telegram supports**. That is deliberately stricter than a naive
``<[a-zA-Z/][^>]*>`` sniff, which misfires on ordinary prose:

  * ``a < b``      — a less-than sign in prose. NOT HTML.
  * ``x <y> z``    — angle brackets around a word. NOT HTML (and would be a
                     Telegram 400 if sent as HTML, since ``<y>`` is not a
                     supported tag).
  * ``5<10``       — arithmetic. NOT HTML.
  * ``<b>x</b>``   — a real tag. HTML.

Requiring *balanced* known tags also rejects half-written markup
(``a <b and b> c``, ``<b>unclosed``) that would be rejected by Telegram.

MIXED CONTENT
-------------
A message may contain both real tags and prose ``<``. When we switch a message
to HTML mode, every ``<`` that is NOT the start of a recognised tag is escaped
to ``&lt;`` — otherwise Telegram's HTML parser would either drop the text or
reject the message outright.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

# Every char MarkdownV2 requires backslash-escaped outside code spans/fences.
#
# NOTE: '<' is deliberately NOT in this class. Telegram's MarkdownV2 reserved set is
# ``_*[]()~`>#+-=|{}.!`` — it contains '>' but NOT '<'. The escaper is therefore
# technically correct as written.
#
# DO NOT "fix" this by adding '<'. That is NOT the bug behind hand-written '<b>'
# showing up as literal tags in a gateway reply. The escaper escapes the '>' of
# '</b>' and leaves the '<' alone, so Telegram renders '<b\\>Bold</b\\>' — the
# user sees the raw tags. The real defect was the MISSING HTML DETECTION in the
# reply path (the reply path hard-coded ParseMode.MARKDOWN_V2 with no tag sniff,
# while the _send_telegram tool sniffed for tags and switched to HTML). That is
# fixed by ``has_html_tags`` + ``TelegramAdapter.format_for_send`` below.
# Escaping '<' here would only hide the tags; it would also corrupt legitimate
# Markdown output (e.g. a code block containing '<html>' would gain a stray
# backslash).
_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')


def escape_markdownv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)


# The tag names Telegram's HTML parse_mode actually supports.
# https://core.telegram.org/bots/api#formatting-options
# Kept in sync with the tag list md2html.py --check rejects (``~/.hermes/scripts/md2html.py``).
_SUPPORTED_TAGS = "b|i|u|s|code|pre|a|tg-spoiler|blockquote"

# A single recognised tag: opening or closing, optionally with attributes.
# Attribute values may contain '>' (e.g. href="a>b"), so the body is [^<>]*?
# and a trailing '>' is required.
_TAG_RE = re.compile(rf"</?(?:{_SUPPORTED_TAGS})\b[^<>]*?>", re.IGNORECASE)

# Opening/closing pairs, used for the balance check.
_PAIR_RE = re.compile(rf"<(/?)({_SUPPORTED_TAGS})\b[^<>]*?>", re.IGNORECASE)


def has_html_tags(text: str) -> bool:
    """True when *text* contains balanced, well-formed, Telegram-supported HTML tags.

    Deliberately stricter than a bare ``<[a-zA-Z/][^>]*>`` sniff so that prose
    containing ``<`` (comparisons, shell redirects, angle-bracketed placeholders)
    is NOT misread as markup — see the module docstring for the case table.
    """
    if not text:
        return False
    if not _TAG_RE.search(text):
        return False
    # Balance check: every opening tag must be closed by a matching closing tag,
    # in order. Unbalanced or crossed markup is not trustworthy as HTML.
    stack: list[str] = []
    for match in _PAIR_RE.finditer(text):
        closing = match.group(1) == "/"
        name = match.group(2).lower()
        if closing:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        else:
            stack.append(name)
    return not stack


def escape_html_text(text: str) -> str:
    """Escape *text* for Telegram's HTML parse_mode, preserving real tags.

    Recognised tags pass through untouched; every other ``<`` becomes ``&lt;``
    so Telegram's HTML parser cannot mistake prose for markup (and reject the
    message with HTTP 400). ``&`` is escaped too — outside a tag it is a
    literal ampersand in Telegram HTML and must be written ``&amp;``.
    """
    if not text:
        return text
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "&":
            out.append("&amp;")
        elif char == "<":
            match = _TAG_RE.match(text, index)
            if match:
                out.append(match.group(0))
                index = match.end()
                continue
            out.append("&lt;")
        elif char == ">":
            # Outside a recognised tag a bare '>' is literal text. Escaping it is
            # optional but harmless, and keeps the output unambiguous.
            out.append("&gt;")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def strip_html_tags(text: str) -> str:
    """Strip HTML tags from *text*, leaving the readable content.

    Used as the plain-text fallback when an HTML-mode message is rejected by
    Telegram's parser: the user still gets the words, not a wall of tags.
    """
    if not text:
        return text
    without_tags = re.sub(r"</?(?:{})\b[^<>]*?>".format(_SUPPORTED_TAGS), "", text, flags=re.IGNORECASE)
    # Unescape the entities escape_html_text() introduced.
    without_tags = without_tags.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return without_tags


def detect_parse_mode(text: str, *, markdown_formatter: Optional[Callable[[str], str]] = None) -> Tuple[str, str]:
    """Return ``(text, mode)`` for *text* — ``mode`` is ``"html"`` or ``"markdown"``.

    *markdown_formatter* converts Markdown to MarkdownV2. It is required only
    when the message is NOT HTML; pass ``None`` to skip conversion entirely
    (the caller then sends the raw text).
    """
    if has_html_tags(text):
        return escape_html_text(text), "html"
    if markdown_formatter is not None:
        return markdown_formatter(text), "markdown"
    return text, "markdown"


def _escape_bare_bracket(m, seg: str) -> str:
    """Escape a bare ( ) { } unless it is already escaped or delimits a ``[text](url)`` link."""
    s = m.start()
    ch = m.group(0)
    if s > 0 and seg[s - 1] == '\\':  # already escaped
        return ch
    if ch == '(' and s > 0 and seg[s - 1] == ']':  # opens a link [text](url)
        return ch
    if ch == ')':  # closes a link URL? walk back matching depth
        before = seg[:s]
        if '](http' in before or '](' in before:
            depth = 0
            for j in range(s - 1, max(s - 2000, -1), -1):
                if seg[j] == '(':
                    depth -= 1
                    if depth < 0:
                        if j > 0 and seg[j - 1] == ']':
                            return ch
                        break
                elif seg[j] == ')':
                    depth += 1
    return '\\' + ch


def markdown_to_markdownv2(content: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2.

    Code is stashed behind placeholders first (never modified), markdown
    constructs become MarkdownV2 syntax, everything else is escaped.

    Lives here rather than on the adapter so the adapter (an over-size file
    under a ratchet that only allows it to shrink) keeps a thin delegating
    wrapper instead of growing.
    """
    if not content:
        return content
    placeholders: dict = {}
    counter = [0]

    def _ph(value: str) -> str:
        """Stash *value* behind a placeholder token that survives escaping."""
        key = f"\x00PH{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = value
        return key

    def _ph_wrap(open_: str, close: str):
        return lambda m: _ph(f"{open_}{escape_markdownv2(m.group(1))}{close}")

    # 0) Rewrite GFM-style pipe tables into Telegram-friendly row groups
    #    before the normal MarkdownV2 conversions run.
    from gateway.platforms.helpers import convert_table_to_bullets as _wrap_markdown_tables

    text = _wrap_markdown_tables(content)

    # 1) Protect fenced code blocks (``` ... ```)
    #    Per MarkdownV2 spec, \ and ` inside pre/code must be escaped.
    #    A fence still opens on its own line — the opening ``` must be the
    #    first triple-backtick run on that line and must end it — but the
    #    line may carry arbitrary leading whitespace (list/blockquote-
    #    nested code indents fences by 4+ spaces) or lead-in prose
    #    ("Here is the code: ```"), both of which the line-start-only
    #    anchor silently downgraded from <pre> to escaped literal text.
    #    Requiring the rest of the opening line to be backtick-free is
    #    what keeps *inline* triple backticks (e.g. "the syntax is
    #    ```x``` inline") out of the match: a closing run can never sit
    #    on the same line as the opener, and the tempered prefix cannot
    #    skip past an earlier run to a later one.  The closing fence must
    #    sit on its own line (any indent), with optional trailing
    #    whitespace and an optional ``\r`` so CRLF-terminated fences
    #    (Windows-authored content) match too.
    def _protect_fenced(m):
        prefix = m.group(1)   # lead-in text / indent before the opening fence
        opening = m.group(2)  # opening ``` (with optional language) and newline
        body = m.group(3)     # code body (may be empty)
        closing = m.group(4)   # closing fence (with its indent)
        body = body.replace('\\', '\\\\').replace('`', '\\`')
        return prefix + _ph(opening + body + closing)

    text = re.sub(
        r'(?m)^((?:(?!```)[^\n])*)(```[^`\n]*\n)([\s\S]*?)(^[ \t]*```)[ \t]*\r?$',
        _protect_fenced,
        text,
    )

    # 2) Protect inline code (`...`)
    #    Escape \ inside inline code per MarkdownV2 spec.
    text = re.sub(
        r'(`[^`]+`)',
        lambda m: _ph(m.group(0).replace('\\', '\\\\')),
        text,
    )

    # 3) Convert markdown links – escape the display text; inside the URL
    #    only ')' and '\' need escaping per the MarkdownV2 spec.
    def _convert_link(m):
        url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
        return _ph(f'[{escape_markdownv2(m.group(1))}]({url})')

    text = re.sub(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _convert_link, text)
    # 4) Headers (## Title) → bold *Title*, stripping redundant ** inside the header
    def _convert_header(m):
        inner = re.sub(r'\*\*(.+?)\*\*', r'\1', m.group(1).strip())
        return _ph(f'*{escape_markdownv2(inner)}*')

    text = re.sub(r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE)
    # 5) Bold **text** → *text*; 6) Italic *text* → _text_ ([^*\n]+ keeps matches on one line, or *
    # bullet lists corrupt); 7) Strikethrough ~~text~~ → ~text~; 8) Spoiler ||text|| kept as-is.
    text = re.sub(r'\*\*(.+?)\*\*', _ph_wrap('*', '*'), text)
    text = re.sub(r'\*([^*\n]+)\*', _ph_wrap('_', '_'), text)
    text = re.sub(r'~~(.+?)~~', _ph_wrap('~', '~'), text)
    text = re.sub(r'\|\|(.+?)\|\|', _ph_wrap('||', '||'), text)
    # 9) Blockquotes: protect leading > from escaping; expandable quotes (**> starts, trailing || ends).
    def _convert_blockquote(m):
        prefix, content = m.group(1), m.group(2)  # prefix: >, >>, >>>, **>, **>> …
        if prefix.startswith('**') and content.endswith('||'):
            return _ph(f'{prefix} {escape_markdownv2(content[:-2])}||')
        return _ph(f'{prefix} {escape_markdownv2(content)}')

    text = re.sub(r'^((?:\*\*)?>{1,3}) (.+)$', _convert_blockquote, text, flags=re.MULTILINE)
    # 10) Escape remaining special characters in plain text
    text = escape_markdownv2(text)
    # 11) Restore placeholders in reverse insertion order so nested placeholders resolve.
    for key in reversed(list(placeholders.keys())):
        text = text.replace(key, placeholders[key])
    # 12) Safety net: escape bare ( ) { } that slipped through, but never inside ``` or ` spans.
    _safe_parts = []
    for _idx, _seg in enumerate(re.split(r'(```[\s\S]*?```|`[^`]+`)', text)):
        if _idx % 2 == 1:
            _safe_parts.append(_seg)  # inside code — untouched
        else:
            _safe_parts.append(re.sub(r'[(){}]', lambda m, _seg=_seg: _escape_bare_bracket(m, _seg), _seg))
    return ''.join(_safe_parts)
