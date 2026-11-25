"""Invariant: the compaction summarizer reads the ORIGINAL tool results.

Why (observed 2026-11-24 on Vision, telegram DM 8943042074): a single turn ran
104 API calls and 16 compactions, re-reading ``provider-quota-wall-card-sweep.md``
TWELVE times — each read immediately after a compaction, while the summary
already named the file.

The mechanism: the compaction pruned tool results to stubs BEFORE the summarizer
ran, so the summarizer wrote the summary from stubs ("[read_file] path=...
(1,234 chars result)") instead of from what was read. The summary therefore
could not record WHICH artifacts had been examined, and the next agent re-read
them. A compaction that loses the evidence it exists to preserve.

Upstream fixed this in ``NousResearch/hermes-agent`` commits ``4a45994971`` (stub
detail) and ``d922b62d28`` (summarize the original tool results first). This
branch forked from upstream on 2026-11-10 and never took either.

These are behaviour contracts: they assert what the summarizer is HANDED, not
that a particular line exists.
"""

from __future__ import annotations

# Imported at MODULE level, like the repo's own tests/agent/test_context_compressor.py:
# importing agent.context_compressor inside a test body would run agent.conversation_loop ->
# hermes_bootstrap -> pm.environments AFTER the conftest sandbox is active, and the payload
# manifest read then trips tests/home_io_guard.py against the real Hermes home.
from agent.context_compressor import ContextCompressor

# `summary_view` does NOT exist on the base commit, so it is imported lazily: a module-level
# import would fail collection on base and the behavioural test below could never prove RED.
# Only the tests that exercise the new API call this.
def _summary_view():
    from agent.context_compressor_window import summary_view
    return summary_view




def _tool_result(call_id: str, text: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": text,
    }


def _assistant_call(call_id: str, name: str = "read_file") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
        ],
    }


def test_summary_view_restores_original_tool_results():
    """The summary view puts the ORIGINAL tool result back for the window."""
    original = _tool_result("call_1", "THE ACTUAL FILE CONTENT — 62 card ids and their dispositions")
    pruned = {**_tool_result("call_1", "[read_file] path=/notes/sweep.md (2,048 chars result)")}

    plan = [_assistant_call("call_1"), pruned]
    originals = [_assistant_call("call_1"), original]

    view = _summary_view()(plan, originals, 0, 2)

    assert view[1]["content"] == original["content"], (
        "the summarizer must read the original tool result, not the stub"
    )
    assert "62 card ids" in view[1]["content"]


def test_summary_view_leaves_non_tool_rows_alone():
    """User/assistant rows are untouched by the summary view."""
    plan = [{"role": "user", "content": "do the thing"}]
    originals = [{"role": "user", "content": "do the thing"}]

    view = _summary_view()(plan, originals, 0, 1)

    assert view[0]["content"] == "do the thing"


def test_summary_view_keeps_clarify_in_its_pruned_form():
    """A clarify stub is a correctness filter, not a size cut — it stays pruned."""
    original = _tool_result("call_c", "the real user answer")
    pruned = {**_tool_result("call_c", "[clarify] using your own judgment")}

    plan = [_assistant_call("call_c", "clarify"), pruned]
    originals = [_assistant_call("call_c", "clarify"), original]

    view = _summary_view()(plan, originals, 0, 2)

    assert view[1]["content"] == "[clarify] using your own judgment"


def test_summary_view_is_a_no_op_on_length_mismatch():
    """A misaligned original list is refused rather than corrupting the window."""
    plan = [{"role": "user", "content": "a"}]
    originals = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]

    view = _summary_view()(plan, originals, 0, 1)

    assert view == plan


# --------------------------------------------- the compressor hands the originals


def test_compressor_summarizer_receives_original_tool_results():
    """compress() hands the summarizer the ORIGINAL tool result, not the stub.

    The real scan rebuilds the summarizer input; the fix must survive that
    rebuild. Before the fix the rebuilt input was the pruned stub.

    Uses the real ContextCompressor and the real ``_plan_compaction_window`` so
    the test exercises the shipped path rather than a re-implementation.

    Fixture notes (each line is load-bearing — see the prune rules):
    - TWO big tool results, with different content. The tail-budget walk breaks at
      the NEWEST oversized row, so the FIRST one falls inside the prunable region
      and is demoted to a stub. One oversized result would break the walk AT itself
      and stay unpruned.
    - Different content so the dedupe pass (which back-references identical
      results) does not claim it — we want the DEMOTE path.
    - A small context_length: the pending tool round is spared from pruning while
      it fits the input window's hard share, and the 256k fallback context length
      makes that share so large nothing is ever pruned.
    """
    from agent.context_compressor import ContextCompressor

    big = "THE ACTUAL CONTENT: card ids A1 A2 A3 with dispositions " + ("x" * 40000)
    other = "SECOND BIG RESULT " + ("y" * 40000)
    messages = [
        {"role": "user", "content": "sweep the cards"},
        _tool_result("call_1", big),
        {"role": "assistant", "content": "sweeping"},
        _tool_result("call_2", other),
        {"role": "assistant", "content": "still working"},
    ]

    comp = ContextCompressor(model="test/model", quiet_mode=True, protect_last_n=1, protect_first_n=1)
    comp.max_tokens = 4096
    comp.context_length = 8192

    canonical = comp._drop_blank_echoes(messages)
    working, pruned_count, cs, ce = comp._plan_compaction_window(messages)

    assert pruned_count >= 1, "the prune must have stubbed the tool result"
    assert "chars result" in working[1]["content"], (
        f"the working copy must carry the stub; got {working[1]['content'][:80]!r}"
    )
    assert "card ids A1 A2 A3" not in working[1]["content"], (
        "the stub must NOT carry the original content"
    )

    # What compress() now does (the fix): the summary view replaces `messages` wholesale, so both the
    # summarizer's window AND the head keep the ORIGINAL rows.
    summary_rows = _summary_view()(working, canonical, cs, ce)
    turns = summary_rows[cs:ce]
    scan = comp._scan_window_handoffs(summary_rows, cs, ce, turns, summary_rows)
    turns = scan.turns_to_summarize

    # The window the summarizer reads carries originals, never stubs.
    summarizer_input = " ".join(
        str(row.get("content", "")) for row in turns if isinstance(row.get("content", ""), str)
    )
    assert "chars result" not in summarizer_input, (
        f"the summarizer must never be handed a stub; it received: {summarizer_input[:200]!r}"
    )

    # And the head — the part assembled into the final transcript — keeps its original too.
    # Without this the pruned row is assembled as a stub and the original is gone for good:
    # this branch's compaction prune does not archive dropped originals to disk.
    assert "card ids A1 A2 A3" in summary_rows[1]["content"], (
        f"the head must keep the original; got {summary_rows[1]['content'][:120]!r}"
    )


def test_summary_view_restores_the_head_too():
    """A pruned row OUTSIDE the window is restored too.

    This branch's compaction prune does not archive the dropped original to disk
    (only the proactive reclaim path does), so a stubbed head row is assembled
    into the final transcript and the original is lost for good. Upstream can
    leave the head pruned because its ``_commit_window`` archives the original to
    ``cache/pruned/``; this branch has neither mechanism, so the original must
    survive in the transcript the summarizer reads.
    """
    original = _tool_result("call_1", "THE ACTUAL FILE CONTENT — 62 card ids")
    pruned = {**_tool_result("call_1", "[read_file] path=/notes/sweep.md (2,048 chars result)")}

    plan = [pruned, {"role": "assistant", "content": "ok"}]
    originals = [original, {"role": "assistant", "content": "ok"}]

    # Window covers only the assistant row; the tool row is in the HEAD.
    view = _summary_view()(plan, originals, 1, 2)

    assert view[0]["content"] == original["content"], (
        "the head's pruned row must be restored: this branch has no archive to "
        "recover it from, so the summary is the only place it can survive"
    )


def test_base_compress_hands_the_summarizer_a_stub():
    """RED on base, GREEN with the fix — and it needs NO new API.

    This is the behavioural invariant, expressed only in terms of code that
    exists on the base commit, so it can be run against base to prove red
    rather than failing at import.

    It drives the real ``compress()`` and inspects the rows the summarizer is
    HANDED. The hook is ``_fallback_anchors`` — the deterministic, provider-free
    consumer of exactly those rows (the same rows the LLM summarizer reads), so
    the test needs no auxiliary provider and cannot pass by accident.

    On base the rows carry ``[read_file] path=... (N chars result)`` — the
    summary is then written from a stub and cannot record which artifact was
    already examined, so the next agent re-reads it. That is the re-grounding
    loop.
    """
    from agent.context_compressor import ContextCompressor

    big = "THE ACTUAL CONTENT: card ids A1 A2 A3 with dispositions " + ("x" * 40000)
    other = "SECOND BIG RESULT " + ("y" * 40000)
    messages = [
        {"role": "user", "content": "sweep the cards"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "/notes/sweep.md"}'}},
        ]},
        _tool_result("call_1", big),
        {"role": "assistant", "content": "sweeping"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_2", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "/notes/other.md"}'}},
        ]},
        _tool_result("call_2", other),
        {"role": "assistant", "content": "still working"},
    ]

    comp = ContextCompressor(model="test/model", quiet_mode=True, protect_last_n=1, protect_first_n=1)
    comp.max_tokens = 4096
    comp.context_length = 8192

    seen: list[str] = []

    def _capture(rows):
        seen.extend(str(r.get("content", "")) for r in rows if isinstance(r.get("content", ""), str))
        return comp._fallback_anchors(rows)

    comp._fallback_anchors = _capture  # type: ignore[method-assign]

    try:
        comp.compress(messages)
    except Exception:
        # The compressor may abort for unrelated reasons in this environment; the
        # invariant is about what the summarizer was HANDED, not about the outcome.
        pass

    assert seen, "the summarizer must have been handed rows to summarize"
    stubbed = [row for row in seen if "chars result" in row]
    assert not stubbed, (
        "the summarizer must never be handed a pruned stub; it received: "
        f"{[r[:120] for r in stubbed]!r}"
    )
    assert any("card ids A1 A2 A3" in row for row in seen), (
        "the summarizer must receive the ORIGINAL tool result; it received: "
        f"{[r[:120] for r in seen]!r}"
    )


def test_summary_stub_recognises_the_chars_result_form():
    """A "(N chars result)" stub is a summary already — it is not summarised again."""
    from agent.context_compressor import _is_summary_stub

    assert _is_summary_stub("[read_file] path=/notes/sweep.md (2,048 chars result)") is True
    assert _is_summary_stub("[terminal] ran `ls` -> exit 0, 3 lines output (400 chars)") is True
