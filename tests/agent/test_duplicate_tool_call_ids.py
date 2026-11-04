"""Regression: a provider tool call emitted TWICE in one turn must run ONCE.

Incident
--------
Session ``ultron/20261104_161428_2a9017`` (2026-11-04, provider ``copilot``,
model ``gpt-6.1-terra``). The recorded assistant turns are in
``~/.hermes/profiles/ultron/state.db`` (``messages.tool_calls``). Each turn
carried every tool call TWICE: once with the real arguments and once with
``{}`` — under the SAME ``id``, the SAME ``call_id`` and the SAME
``response_item_id`` (the provider's per-item identity).

Measured over the recorded turns: 45 entries, 17 duplicated item ids, and
EVERY duplicated item id had exactly one non-empty-args copy and one
empty-args copy. Both copies were dispatched, so each call ran twice — once
usefully and once as a malformed empty-argument call, which is what tripped
the no-progress guardrail (the turn ended ``reason=guardrail_halt``).

What this suite pins
--------------------
* ONE provider item represented twice collapses to ONE dispatched call, and
  the copy with the COMPLETE arguments is the one that survives.
* The empty copy is never dispatched (no duplicate side effect).
* Calls with DIFFERENT provider item identities are distinct operations and
  ALL of them run — including two legitimate calls to the same tool with
  byte-identical arguments, and zero-argument calls.
* The id repair stays atomic: a failing write rolls back and never leaves a
  half-renamed (split-brain) entry.
* The whole path is exercised from the STREAM (raw SSE events) through
  validation to dispatch, with a fake side-effect counter.
"""

from types import SimpleNamespace

import pytest

from agent.message_sanitization import (
    coalesce_tool_call_id,
    deduplicate_tool_calls,
    provider_item_identity,
    reconcile_duplicate_tool_call_items,
    uniquify_tool_call_ids,
)
from agent.transports.types import ToolCall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_call(call_id, name, arguments="{}", item_id=None, tool_id=None):
    """A ToolCall shaped like the Codex transport builds it.

    ``response_item_id`` is the provider's per-item identity; the transport
    derives it from the call id, so a repeated call carries the same one.
    """
    provider_data = {}
    if call_id:
        provider_data["call_id"] = call_id
    if item_id:
        provider_data["response_item_id"] = item_id
    return ToolCall(
        id=tool_id if tool_id is not None else call_id,
        name=name,
        arguments=arguments,
        provider_data=provider_data or None,
    )


def _duplicate_pair(call_id, name, arguments, item_id=None):
    """The incident shape: one provider item, emitted twice (full args + ``{}``)."""
    return [
        _tool_call(call_id, name, arguments, item_id=item_id),
        _tool_call(call_id, name, "{}", item_id=item_id),
    ]


class SideEffectCounter:
    """Fake side-effect sink: one entry per dispatched call."""

    def __init__(self):
        self.effects = []

    def dispatch(self, tool_calls):
        for tc in tool_calls:
            self.effects.append((tc.function.name, tc.function.arguments))
        return self.effects


def _run_pipeline(tool_calls):
    """The real pipeline order (agent/turn_tool_validation.py + turn_tool_round.py)."""
    tool_calls = reconcile_duplicate_tool_call_items(tool_calls)
    uniquify_tool_call_ids(tool_calls)
    return deduplicate_tool_calls(tool_calls)


# ---------------------------------------------------------------------------
# The incident: one provider item emitted twice runs ONCE
# ---------------------------------------------------------------------------

class TestOneItemEmittedTwiceRunsOnce:
    def test_valid_plus_empty_same_item_collapses_to_one_call(self):
        """The incident shape. Exactly one call survives and it carries the
        COMPLETE arguments — never the empty copy."""
        calls = _duplicate_pair("call_A", "skill_view", '{"name":"hermes-profile-state-ownership"}')
        kept = _run_pipeline(calls)
        assert len(kept) == 1
        assert kept[0].function.arguments == '{"name":"hermes-profile-state-ownership"}'

    def test_empty_copy_is_never_dispatched(self):
        """The duplicate side effect: the empty copy must not reach dispatch."""
        calls = _duplicate_pair("call_A", "search_files", '{"path":"/tmp/x","pattern":"SOUL.md"}')
        counter = SideEffectCounter()
        counter.dispatch(_run_pipeline(calls))
        assert counter.effects == [("search_files", '{"path":"/tmp/x","pattern":"SOUL.md"}')]

    def test_both_orders_collapse_to_the_full_argument_copy(self):
        """The empty copy may come FIRST in the batch. The surviving call must
        still be the one with the real arguments."""
        full = _tool_call("call_A", "terminal", '{"command":"pwd"}')
        empty = _tool_call("call_A", "terminal", "{}")
        for order in ([full, empty], [empty, full]):
            kept = _run_pipeline(list(order))
            assert len(kept) == 1, f"expected one call, got {len(kept)}"
            assert kept[0].function.arguments == '{"command":"pwd"}'

    def test_three_copies_of_one_item_collapse_to_one(self):
        calls = [
            _tool_call("call_A", "web_extract", '{"urls":["https://x"]}'),
            _tool_call("call_A", "web_extract", "{}"),
            _tool_call("call_A", "web_extract", "{}"),
        ]
        kept = _run_pipeline(calls)
        assert len(kept) == 1
        assert kept[0].function.arguments == '{"urls":["https://x"]}'

    def test_collapsed_call_keeps_its_id(self):
        """The survivor keeps the provider's id so its result pairs correctly."""
        calls = _duplicate_pair("call_A", "search_files", '{"path":"/tmp/x"}')
        kept = _run_pipeline(calls)
        assert coalesce_tool_call_id(kept[0]) == "call_A"
        assert kept[0].call_id == "call_A"


# ---------------------------------------------------------------------------
# Distinct item identities are DISTINCT operations and must all run
# ---------------------------------------------------------------------------

class TestDistinctItemsAllRun:
    def test_two_identical_arguments_different_items_both_run(self):
        """Two legitimate calls to the same tool with byte-identical arguments
        are TWO operations. Dropping the second would discard a call the model
        asked for."""
        calls = [
            _tool_call("call_1", "write_file", '{"path":"/tmp/a"}', item_id="fc_1"),
            _tool_call("call_2", "write_file", '{"path":"/tmp/a"}', item_id="fc_2"),
        ]
        counter = SideEffectCounter()
        counter.dispatch(_run_pipeline(calls))
        assert len(counter.effects) == 2
        assert counter.effects == [("write_file", '{"path":"/tmp/a"}')] * 2

    def test_two_identical_arguments_different_items_get_distinct_ids(self):
        """Both must be able to pair with their own result."""
        calls = [
            _tool_call("call_1", "write_file", '{"path":"/tmp/a"}', item_id="fc_1"),
            _tool_call("call_2", "write_file", '{"path":"/tmp/a"}', item_id="fc_2"),
        ]
        kept = _run_pipeline(calls)
        ids = [coalesce_tool_call_id(tc) for tc in kept]
        assert len(set(ids)) == 2

    def test_zero_argument_calls_to_different_tools_both_run(self):
        """``{}`` is legitimate; two different zero-argument tools both run."""
        calls = [
            _tool_call("call_1", "kanban_show", "{}", item_id="fc_1"),
            _tool_call("call_2", "list_dir", "{}", item_id="fc_2"),
        ]
        counter = SideEffectCounter()
        counter.dispatch(_run_pipeline(calls))
        assert len(counter.effects) == 2

    def test_a_lone_zero_argument_call_survives(self):
        """The incident's single ``kanban_show {}`` had NO duplicate and must run."""
        calls = [_tool_call("call_NMFQ", "kanban_show", "{}", item_id="fc_NMFQ")]
        counter = SideEffectCounter()
        counter.dispatch(_run_pipeline(calls))
        assert counter.effects == [("kanban_show", "{}")]

    def test_same_item_zero_argument_pair_collapses(self):
        """One item emitted twice as ``{}`` + ``{}`` is still ONE operation."""
        calls = [
            _tool_call("call_A", "kanban_show", "{}", item_id="fc_A"),
            _tool_call("call_A", "kanban_show", "{}", item_id="fc_A"),
        ]
        kept = _run_pipeline(calls)
        assert len(kept) == 1

    def test_distinct_items_sharing_one_call_id_both_run(self):
        """The provider reused a call id across two DIFFERENT items. Both are
        real operations; the id repair gives the second its own id."""
        calls = [
            _tool_call("call_A", "search_files", '{"path":"/tmp/a"}', item_id="fc_1"),
            _tool_call("call_A", "search_files", '{"path":"/tmp/b"}', item_id="fc_2"),
        ]
        counter = SideEffectCounter()
        counter.dispatch(_run_pipeline(calls))
        assert len(counter.effects) == 2
        kept = _run_pipeline(calls)
        ids = [coalesce_tool_call_id(tc) for tc in kept]
        assert len(set(ids)) == 2, f"ids still collide: {ids}"


# ---------------------------------------------------------------------------
# The id repair itself: atomic, total, and never split-brained
# ---------------------------------------------------------------------------

class TestIdRepairIsAtomicAndTotal:
    def test_writable_call_id_is_rewritten_in_every_spelling(self):
        """A property WITH a setter must not leave the view and the store apart."""
        calls = [
            _tool_call("call_A", "search_files", '{"path":"/tmp/a"}'),
            _tool_call("call_A", "search_files", '{"path":"/tmp/b"}'),
        ]
        uniquify_tool_call_ids(calls)
        for tc in calls:
            assert tc.id == tc.call_id, f"split-brain: id={tc.id!r} call_id={tc.call_id!r}"
            assert tc.provider_data["call_id"] == tc.call_id

    def test_failing_write_rolls_back_completely(self):
        """A call whose call_id write raises must keep EVERY original spelling.

        Before the fix the ``id`` was already rewritten when the ``call_id``
        write raised, leaving a split-brain entry whose effective id was still
        the duplicated one.
        """
        class _Exploding:
            def __init__(self):
                self.id = "call_A"
                self.function = SimpleNamespace(name="boom", arguments="{}")

            @property
            def call_id(self):
                return "call_A"

            @property
            def provider_data(self):
                raise RuntimeError("provider_data unavailable")

        tc = _Exploding()
        uniquify_tool_call_ids([tc])
        assert tc.id == "call_A", "entry was left half-renamed after a failed write"

    def test_failing_write_does_not_reserve_the_suffix(self):
        class _Exploding:
            def __init__(self):
                self.id = "call_Z"
                self.function = SimpleNamespace(name="boom", arguments="{}")

            @property
            def call_id(self):
                return "call_Z"

            @property
            def provider_data(self):
                raise RuntimeError("provider_data unavailable")

        exploding = _Exploding()
        healthy = _tool_call("call_Z", "after", '{"path":"/tmp/a"}')
        uniquify_tool_call_ids([exploding, healthy])
        assert healthy.id == "call_Z_d2"

    def test_suffix_collision_advances_the_counter(self):
        calls = [
            _tool_call("z", "a", '{"i":1}'),
            _tool_call("z_d2", "b", '{"i":2}'),
            _tool_call("z", "c", '{"i":3}'),
        ]
        uniquify_tool_call_ids(calls)
        assert calls[2].id == "z_d3"

    def test_response_item_alias_survives_the_rename(self):
        """The provider's ``fc_``/item id must survive an id rename."""
        calls = [
            _tool_call("call_A", "search_files", '{"path":"/tmp/a"}', item_id="fc_A"),
            _tool_call("call_A", "search_files", '{"path":"/tmp/b"}', item_id="fc_A"),
        ]
        uniquify_tool_call_ids(calls)
        assert calls[0].response_item_id == "fc_A"
        assert calls[1].response_item_id == "fc_A"

    def test_composite_id_keeps_its_item_half(self):
        calls = [
            {"id": "call_y|fc_1", "function": {"name": "f", "arguments": "{}"}},
            {"id": "call_y|fc_2", "function": {"name": "g", "arguments": "{}"}},
        ]
        uniquify_tool_call_ids(calls)
        assert calls[0]["id"] == "call_y|fc_1"
        assert calls[1]["id"] == "call_y_d2|fc_2"


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

class TestProviderItemIdentity:
    def test_response_item_id_wins_over_id(self):
        tc = _tool_call("call_A", "f", "{}", item_id="fc_A")
        assert provider_item_identity(tc) == "fc_A"

    def test_falls_back_to_the_entry_id(self):
        tc = _tool_call("call_A", "f", "{}")
        assert provider_item_identity(tc) == "call_A"

    def test_blank_identity_is_empty_string(self):
        tc = SimpleNamespace(id="", call_id=None, function=None)
        assert provider_item_identity(tc) == ""


# ---------------------------------------------------------------------------
# Stream -> validation -> dispatch, with a fake side-effect counter
# ---------------------------------------------------------------------------

class TestStreamToDispatch:
    def _stream_events(self, items):
        """Build the raw Responses SSE events for a list of function-call items."""
        events = []
        for index, item in enumerate(items):
            events.append(SimpleNamespace(
                type="response.output_item.added", output_index=index, item=item,
            ))
        events.append(SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(id="resp_1", usage=None, status="completed"),
        ))
        return events

    def test_stream_emitting_one_item_twice_keeps_the_real_arguments(self):
        """The full path: raw SSE events -> assembled output -> validation ->
        dispatch.

        The stream announces the SAME provider item twice (once with its real
        arguments, once with ``{}``). The assembler must keep the RICHEST
        arguments for that item — overwriting would make the call run with no
        arguments — and the pipeline must dispatch it exactly ONCE.
        """
        from agent.codex_runtime import _consume_codex_event_stream

        events = self._stream_events([
            SimpleNamespace(type="function_call", id="fc_A", call_id="call_A",
                             name="skill_view", arguments='{"name":"x"}'),
            SimpleNamespace(type="function_call", id="fc_A", call_id="call_A",
                             name="skill_view", arguments=""),
        ])
        result = _consume_codex_event_stream(events, model="gpt-5")
        function_calls = [i for i in result.output if getattr(i, "type", "") == "function_call"]
        assert len(function_calls) == 1, "one provider item must yield one function call"
        assert function_calls[0].arguments == '{"name":"x"}', (
            "the empty copy overwrote the real arguments"
        )

        # The assembled entry carries the provider's item id and call id.
        tool_calls = [
            _tool_call(fc.call_id, fc.name, fc.arguments, item_id=fc.id)
            for fc in function_calls
        ]
        counter = SideEffectCounter()
        counter.dispatch(_run_pipeline(tool_calls))
        assert len(counter.effects) == 1
        assert counter.effects[0][1] == '{"name":"x"}'

    def test_stream_empty_copy_first_keeps_the_real_arguments(self):
        """The empty copy may arrive FIRST in the stream."""
        from agent.codex_runtime import _consume_codex_event_stream

        events = self._stream_events([
            SimpleNamespace(type="function_call", id="fc_A", call_id="call_A",
                             name="skill_view", arguments=""),
            SimpleNamespace(type="function_call", id="fc_A", call_id="call_A",
                             name="skill_view", arguments='{"name":"x"}'),
        ])
        result = _consume_codex_event_stream(events, model="gpt-5")
        function_calls = [i for i in result.output if getattr(i, "type", "") == "function_call"]
        assert len(function_calls) == 1
        assert function_calls[0].arguments == '{"name":"x"}'

    def test_stream_with_distinct_items_dispatches_both(self):
        """Two genuinely different stream items both dispatch."""
        from agent.codex_runtime import _consume_codex_event_stream

        events = self._stream_events([
            SimpleNamespace(type="function_call", id="fc_1", call_id="call_1",
                             name="search_files", arguments='{"path":"/tmp/a"}'),
            SimpleNamespace(type="function_call", id="fc_2", call_id="call_2",
                             name="search_files", arguments='{"path":"/tmp/a"}'),
        ])
        result = _consume_codex_event_stream(events, model="gpt-5")
        function_calls = [i for i in result.output if getattr(i, "type", "") == "function_call"]
        tool_calls = [
            _tool_call(fc.call_id, fc.name, fc.arguments, item_id=fc.id)
            for fc in function_calls
        ]
        counter = SideEffectCounter()
        counter.dispatch(_run_pipeline(tool_calls))
        assert len(counter.effects) == 2

    def test_terminal_output_without_function_calls_is_unchanged(self):
        """A text-only stream produces no tool calls and must be untouched."""
        from agent.codex_runtime import _consume_codex_event_stream

        events = [
            SimpleNamespace(type="response.output_text.delta", delta="hello"),
            SimpleNamespace(type="response.completed",
                             response=SimpleNamespace(id="resp_1", usage=None, status="completed")),
        ]
        result = _consume_codex_event_stream(events, model="gpt-5")
        assert not [i for i in result.output if getattr(i, "type", "") == "function_call"]
        assert result.output_text == "hello"


# ---------------------------------------------------------------------------
# The argument-level dedup keeps its documented behaviour
# ---------------------------------------------------------------------------

class TestArgumentLevelDedup:
    def test_zero_argument_calls_to_different_tools_both_survive(self):
        calls = [
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="read_file", arguments="{}")),
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="list_dir", arguments="{}")),
        ]
        kept = deduplicate_tool_calls(calls)
        assert len(kept) == 2

    def test_same_tool_same_args_without_identity_collapses(self):
        """With no identity at all, the argument-level rule still applies."""
        calls = [
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="read_file", arguments='{"path":"/x"}')),
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="read_file", arguments='{"path":"/x"}')),
        ]
        kept = deduplicate_tool_calls(calls)
        assert len(kept) == 1

    def test_key_order_variants_collapse(self):
        calls = [
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="read_file",
                                                     arguments='{"path":"/x","limit":5}')),
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="read_file",
                                                     arguments='{"limit": 5, "path": "/x"}')),
        ]
        kept = deduplicate_tool_calls(calls)
        assert len(kept) == 1

    def test_same_tool_different_args_both_survive(self):
        calls = [
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="read_file", arguments='{"path":"/x"}')),
            SimpleNamespace(id=None, call_id=None,
                            function=SimpleNamespace(name="read_file", arguments='{"path":"/y"}')),
        ]
        kept = deduplicate_tool_calls(calls)
        assert len(kept) == 2
