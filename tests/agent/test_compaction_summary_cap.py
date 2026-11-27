"""Invariants: a compaction summary may not grow past its budget, and may not
re-trigger compaction by dominating the live transcript.

Observed 2026-11-25/26 on Vision (profile ``default``, telegram DM 8943042074),
session ``20261110_162723_dc65d8e8``: 157 compactions, summaries growing
59,597 -> 135,803 chars, compaction firing every 4-14 minutes for 16 days
(2,263 API calls, 291M cache-read tokens).

The mechanism, measured:

* The summarizer is TOLD to target ``max_summary_tokens`` (10,000) but the
  budget is only a suggestion in the prompt. Live summaries reached 114,050
  chars (~28,500 tokens) -- a 3x overshoot.
* On the iterative-update path the PREVIOUS summary is fed back in
  (``PREVIOUS SUMMARY: {...}`` / ``NEW TURNS TO INCORPORATE: {...}``) with the
  instruction "PRESERVE all existing information that is still relevant", so
  the summary is monotonically non-shrinking by construction.
* The committed summary row is 114,050 of the session's 161,507 live chars --
  70.6% of the whole prompt context. A dozen ordinary turns then re-cross the
  compaction threshold and the cycle repeats.

Two separate defects, two invariants:

A. The published summary is capped at a budget, so it cannot grow without
   bound and cannot dominate the live transcript.
C. Compaction is refused when the live transcript is ALREADY summary-dominated:
   compacting again would replace one oversized summary with another, which is
   what feeds the loop.

These are behaviour contracts. They assert the OBSERVABLE outcome (the size of
what gets published, and whether a compaction is refused), not that a
particular line exists.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor


# ---------------------------------------------------------------- helpers


def _assistant_turns(n: int = 6) -> list[dict]:
    """Turns the summarizer can consume.

    Deliberately assistant-role only. A ``user``-role row makes the summarizer
    call ``_is_synthetic_compression_user_turn``, which lazily imports
    ``agent.conversation_loop`` -> ``hermes_bootstrap`` -> ``pm`` and reads
    ``$HERMES_HOME/manifest.json``. That trips ``tests/home_io_guard.py`` in
    this environment and is unrelated to what these invariants assert.
    """
    return [{"role": "assistant", "content": f"work happened {i} " + "y" * 200} for i in range(n)]


def _compressor(**kw) -> ContextCompressor:
    params = dict(
        model="MiniMax-M3",
        threshold_percent=0.5,
        protect_first_n=1,
        protect_last_n=2,
        summary_target_ratio=0.2,
        config_context_length=1_000_000,
    )
    params.update(kw)
    return ContextCompressor(**params)


# ---------------------------------------------------------------- A: the cap


class TestSummarySizeCap:
    """A. The summary that gets PUBLISHED is bounded by a budget."""

    def test_an_oversized_summary_is_capped_before_it_is_published(self):
        """A summarizer that ignores its token budget must not produce an unbounded summary.

        Red on base: the raw 120,000-char summary is returned verbatim.
        Green on fix: the published summary is capped.
        """
        comp = _compressor()
        huge = "PRESERVED DETAIL. " * 6000  # ~120,000 chars, ~12x the token budget
        assert len(huge) > 60_000, "fixture must exceed the cap to be meaningful"

        with patch.object(comp, "_call_summary_llm", return_value=huge), patch.object(
            comp, "_validate_summary_user_provenance"
        ):
            summary = comp._generate_summary(_assistant_turns(), None)

        assert len(summary) <= comp.max_summary_chars + 2_000, (
            f"published summary is {len(summary)} chars, over the {comp.max_summary_chars} cap"
        )

    def test_the_cap_marks_what_it_dropped(self):
        """A capped summary must not look like a complete one.

        An unmarked truncation reads as a full summary and silently loses the
        tail (which carries 'what to do next'). The cap must leave an elision
        marker so the next agent knows information was dropped.
        """
        comp = _compressor()
        huge = "PRESERVED DETAIL. " * 6000

        with patch.object(comp, "_call_summary_llm", return_value=huge), patch.object(
            comp, "_validate_summary_user_provenance"
        ):
            summary = comp._generate_summary(_assistant_turns(), None)

        assert "omitted" in summary.lower(), "a capped summary must mark the elision"

    def test_a_summary_inside_the_budget_is_untouched(self):
        """The cap must not touch a summary that already fits -- no elision marker, no loss."""
        comp = _compressor()
        small = "## Completed\n- read the report\n- wrote the patch\n"

        with patch.object(comp, "_call_summary_llm", return_value=small), patch.object(
            comp, "_validate_summary_user_provenance"
        ):
            summary = comp._generate_summary(_assistant_turns(), None)

        assert small.strip() in summary
        assert "omitted" not in summary.lower()

    def test_the_cap_is_derived_from_the_token_budget(self):
        """The cap must track ``max_summary_tokens``, not a hardcoded char count.

        A summary is ~4 chars/token; the budget is 10,000 tokens, so the cap
        must be a multiple of that, with headroom for the token->char skew.
        """
        comp = _compressor()
        assert comp.max_summary_chars >= comp.max_summary_tokens * 4, (
            "the char cap must be at least the token budget in chars"
        )
        assert comp.max_summary_chars <= comp.max_summary_tokens * 8, (
            "the char cap must not be so loose that a 3x overshoot still fits"
        )

    def test_the_capped_summary_is_what_becomes_the_iterative_base(self):
        """The cap must apply to ``_previous_summary``, not only to the published row.

        ``_previous_summary`` is what the next update feeds back in. A cap applied
        only at publication would leave the unbounded value in the iterative
        base, and the next summary would re-expand from it.
        """
        comp = _compressor()
        huge = "PRESERVED DETAIL. " * 6000

        with patch.object(comp, "_call_summary_llm", return_value=huge), patch.object(
            comp, "_validate_summary_user_provenance"
        ):
            comp._generate_summary(_assistant_turns(), None)

        assert comp._previous_summary is not None
        assert len(comp._previous_summary) <= comp.max_summary_chars + 2_000, (
            f"the iterative base is {len(comp._previous_summary)} chars, over the cap"
        )

    def test_the_iterative_update_input_is_capped_too(self):
        """The previous summary handed back to the summarizer must be bounded.

        The update path feeds PREVIOUS SUMMARY (up to 160,000 chars) plus the
        new turns (up to 160,000 chars). Feeding back a 135,803-char summary is
        what makes the next summary larger than the last.
        """
        comp = _compressor()
        comp._previous_summary = "OLD DETAIL. " * 12000  # ~132,000 chars

        bounded = comp._bound_summary_input(comp._previous_summary, limit=comp._previous_summary_max_chars)

        assert len(bounded) <= comp._previous_summary_max_chars, (
            f"previous-summary input is {len(bounded)} chars, over the "
            f"{comp._previous_summary_max_chars} bound"
        )


# ---------------------------------------------------------------- C: the brake


class TestSummaryDominanceGate:
    """C. Refuse to compact when the live transcript is already summary-dominated."""

    def test_a_summary_dominated_transcript_refuses_to_compact(self):
        """A live transcript that is mostly one summary must not be compacted again.

        Compacting it again replaces one oversized summary with another
        oversized summary -- which is exactly the feedback loop. The gate must
        refuse with a reason.
        """
        comp = _compressor()
        comp._last_summary_chars = 114_050
        comp._last_live_chars = 161_507

        should, reason = comp.should_compress_info(prompt_tokens=200_000)

        assert should is False, "compaction must be refused on a summary-dominated transcript"
        assert reason and "summary" in reason.lower(), f"the refusal must name the cause, got {reason!r}"

    def test_the_brake_does_not_short_circuit_the_feasibility_probe(self):
        """The dominance brake defers a compaction; it must not suppress a diagnostic.

        The lazy feasibility probe warns the user that their summariser window is
        below the minimum, and a hard rejection has to reach them. The pre-probe
        gate therefore evaluates WITHOUT the dominance brake, and the brake is
        re-evaluated after the probe -- where deferring costs nothing.

        Red on base: the flag does not exist, so the pre-probe gate always
        consults every brake.
        """
        comp = _compressor()
        comp._last_summary_chars = 114_050
        comp._last_live_chars = 161_507

        # The brake DOES block when it is consulted.
        assert comp._automatic_compression_blocked() is True, (
            "the dominance brake must block when it is evaluated"
        )

        # ...but it is excluded from the pre-probe evaluation.
        assert comp._automatic_compression_blocked(exclude_summary_dominance=True) is False, (
            "the pre-probe gate must not short-circuit on the dominance brake"
        )

    def test_excluding_the_brake_does_not_disable_the_other_guards(self):
        """Excluding the dominance brake must not weaken the interval or the loop guard.

        The flag is targeted: a session inside its cooldown floor must still be
        deferred, otherwise the exclusion would open a hole.
        """
        comp = _compressor(min_interval_seconds=600)
        comp._last_compression_completed_at = time.monotonic()

        assert comp._automatic_compression_blocked(exclude_summary_dominance=True) is True, (
            "the interval guard must still block when the dominance brake is excluded"
        )

    def test_a_normal_transcript_still_compacts(self):
        """The gate must not block ordinary compaction -- only summary-dominated ones."""
        comp = _compressor()
        comp._last_summary_chars = 8_000
        comp._last_live_chars = 120_000

        should, reason = comp.should_compress_info(prompt_tokens=200_000)

        assert should is True, f"ordinary compaction must still be allowed, got reason {reason!r}"

    def test_a_transcript_with_no_summary_still_compacts(self):
        """With no summary in the transcript there is nothing to be dominated by."""
        comp = _compressor()
        comp._last_summary_chars = 0
        comp._last_live_chars = 300_000

        should, reason = comp.should_compress_info(prompt_tokens=200_000)

        assert should is True, f"compaction must be allowed, got reason {reason!r}"

    def test_the_gate_uses_the_share_not_the_absolute_size(self):
        """A big summary inside a big transcript is fine; only the SHARE matters.

        60,000 chars of summary in a 500,000-char transcript is 12% -- normal.
        114,050 in 161,507 is 70.6% -- pathological. The gate must compare the
        two, not trip on either alone.
        """
        comp = _compressor()
        comp._last_summary_chars = 60_000
        comp._last_live_chars = 500_000

        should, _ = comp.should_compress_info(prompt_tokens=200_000)
        assert should is True, "a 12% summary share must not trip the gate"

    def test_the_gate_is_scale_free(self):
        """The same 70.6% share must trip whether the transcript is small or large.

        This is the property the absolute-size guard lacks: Vision's transcript
        was only 161,507 chars and still pathological.
        """
        comp = _compressor()
        comp._last_summary_chars = 700_000
        comp._last_live_chars = 1_000_000

        should, reason = comp.should_compress_info(prompt_tokens=200_000)
        assert should is False, "a 70% share must trip regardless of absolute size"
        assert reason and "summary" in reason.lower()


# ---------------------------------------------------------------- the cadence brake


class TestCompactionIntervalBrake:
    """C (cadence). The minimum interval between compactions must be long enough
    to stop a 4-minute compaction cycle, while still allowing a genuine
    full-context session to compact more than once per hour.

    The floor is a CONFIG knob (``compression.min_interval_seconds``), not a
    hardcoded constant, so a profile that shows the loop can be tightened
    without a code change and without touching the rest of the fleet.
    """

    def test_the_interval_is_a_config_knob_not_a_hardcoded_constant(self):
        """The effective floor must come from config, not from a class constant.

        A hardcoded 600s would change every profile's compaction cadence at
        once. The knob lets Vision be tightened first and the fleet to follow
        once the measurement supports it.
        """
        comp = _compressor(min_interval_seconds=600)
        assert comp._effective_min_interval_seconds() == 600, (
            "the configured interval must be the effective one"
        )

    def test_a_profile_can_widen_the_interval_without_a_code_change(self):
        """A profile that shows the loop raises its own floor; the default is untouched."""
        default = _compressor()
        tightened = _compressor(min_interval_seconds=600)
        assert tightened._effective_min_interval_seconds() > default._effective_min_interval_seconds()

    def test_the_default_floor_is_the_historical_two_minutes(self):
        """The shipped default must not silently change the fleet's cadence.

        Raising the class constant would alter every profile at deploy time.
        The default stays at the historical 120s; the knob is how a profile
        opts into a longer floor.
        """
        assert ContextCompressor._MIN_COMPACTION_INTERVAL_SECONDS == 120.0, (
            "the built-in default must stay at the historical 120s so the fleet's "
            "compaction cadence is unchanged until a profile opts in"
        )

    def test_a_zero_interval_disables_the_floor(self):
        """0 means 'no floor' -- a profile can opt out entirely."""
        comp = _compressor(min_interval_seconds=0)
        assert comp._effective_min_interval_seconds() == 0.0
        comp._last_compression_completed_at = time.monotonic()
        assert comp._compression_interval_elapsed() is True, (
            "a zero floor must not defer a compaction"
        )

    def test_a_second_compression_inside_the_interval_is_refused(self):
        """The interval must actually be enforced against a real timestamp."""
        comp = _compressor(min_interval_seconds=600)
        comp._last_compression_completed_at = 1_000.0  # a long time ago

        # First check: far in the past -> allowed.
        assert comp._compression_interval_elapsed() is True

        # Now pretend one just completed.
        comp._last_compression_completed_at = time.monotonic()
        assert comp._compression_interval_elapsed() is False
