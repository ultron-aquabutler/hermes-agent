"""The usage-independent compaction-loop detector must fire on a real loop.

Background
----------
``should_compress()`` gates on the provider's REAL ``prompt_tokens``. MiniMax omits
usage, so the anti-thrash breaker -- whose verdict arrives in
``update_from_response()`` -- never receives a reading and never trips. The session
then compacts forever (the runaway: 56 compactions in one session, every 9-11
minutes).

This detector substitutes a signal that does not need provider usage. TWO signals
are combined, and both are available at the compaction boundary without usage:

* ``rough_tokens`` vs ``threshold_tokens`` -- is the request still over the line?
* ``_last_compression_savings_pct`` -- did THIS compaction actually reclaim
  anything? This is the messages-vs-messages shrinkage ``compress()`` already
  computes, so it measures what compaction can actually shrink.

Why both
--------
The first cut of this detector tripped on "the estimate is still over the
threshold". Measured against the real runaway session (``20261110_162723_dc65d8e8``,
24 compaction boundaries) that is wrong: the session's legitimate early boundaries
sat at 1.41M tokens -- roughly 11x over the 120K threshold -- purely because its
schema-heavy tool set is enormous, while each of those compactions was still
reclaiming 0.5%-11.6% of its message tokens. Keying on size alone punishes a large
but progressing session.

Conversely, keying on message savings alone would miss the case where the
incompressible floor (system prompt + tool schemas) is what is over the line, and
the messages are already tiny.

Together they isolate the actual failure: the request is still over the line AND
compaction is no longer reclaiming anything.

The measured boundary table below is REAL data from that session -- each row is the
live payload (compaction summary + non-compacted tail) at that boundary, with
``estimate_request_tokens_rough`` on the same basis as ``threshold_tokens``. The
savings column is the messages-vs-messages shrinkage between consecutive
boundaries. These tests replay it so the tuning cannot drift away from the data
it was tuned on.
"""
import time

import pytest

from agent.context_compressor import ContextCompressor


def _compressor(threshold_tokens: int = 120_000) -> ContextCompressor:
    cc = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=5,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=40960,
        provider="test",
    )
    cc.threshold_tokens = threshold_tokens  # pin; don't couple to window math
    cc._generate_summary = lambda *a, **k: "Summary of earlier turns."
    return cc


# (rough_estimate, message_savings_pct, payload_message_count) at each of the 24
# compaction boundaries of session 20261110_162723_dc65d8e8.
REAL_RUNAWAY_BOUNDARIES = [
    (1_410_169, 60.7, 1448),   # 1
    (1_329_152, 5.7, 1280),    # 2
    (1_329_148, 0.0, 1280),    # 3  <- loop starts: nothing reclaimed
    (1_329_147, 0.0, 1280),    # 4  <- loop
    (1_329_153, 0.0, 1280),    # 5  <- loop
    (1_329_111, 0.0, 1279),    # 6  <- loop
    (1_322_898, 0.5, 1264),    # 7  <- loop
    (1_169_496, 11.6, 1062),   # 8  <- progress resumes
    (1_154_687, 1.3, 1046),    # 9
    (1_139_132, 1.3, 1023),    # 10
    (1_131_045, 0.7, 1014),    # 11
    (1_125_648, 0.5, 1003),    # 12
    (1_117_731, 0.7, 976),     # 13
    (1_109_614, 0.7, 945),     # 14
    (1_099_606, 0.9, 936),     # 15
    (574_913, 47.7, 573),      # 16  <- big progress
    (570_262, 0.8, 561),       # 17
    (124_142, 78.2, 70),       # 18  <- big progress, just over threshold
    (116_131, 6.5, 64),        # 19  <- now UNDER threshold
    (72_913, 37.2, 49),        # 20
    (47_938, 34.3, 42),        # 21
    (46_800, 2.4, 39),         # 22
    (18_068, 61.4, 23),        # 23
    (16_930, 6.3, 20),         # 24
]


def _replay(cc: ContextCompressor, boundaries) -> list:
    """Replay boundary rows through the detector; return the per-boundary state."""
    states = []
    for est, savings, _msgs in boundaries:
        cc._last_compression_savings_pct = savings
        cc._record_usage_less_loop_verdict(est)
        states.append((est, savings, cc._usage_less_loop_count, cc._usage_less_loop_tripped()))
    return states


class TestRealRunawayReplay:
    def test_real_runaway_trips(self):
        """Replaying the REAL boundary sequence must trip the detector.

        Boundaries 3-7 reclaim 0.0%-0.5% of message tokens while the estimate sits
        at ~1.33M -- an order of magnitude over the 120K threshold. That is the loop,
        and the detector must catch it.

        Note the assertion is on the FIRST trip DURING the replay, not on the end
        state: the runaway later recovered (boundary 19 landed under the threshold),
        and a detector that latched forever would be the very permanence this fix
        is meant to avoid.
        """
        cc = _compressor()
        states = _replay(cc, REAL_RUNAWAY_BOUNDARIES)
        tripped_at = [i for i, s in enumerate(states, 1) if s[3]]
        assert tripped_at, (
            f"the real runaway's boundary sequence must trip the detector; states={states}"
        )
        assert tripped_at[0] == 4, (
            f"must trip on the second no-progress boundary (boundary 4); "
            f"tripped at {tripped_at}"
        )

    def test_real_runaway_trips_on_the_no_progress_stretch(self):
        """It must trip by boundary 4 -- the second boundary that reclaimed nothing."""
        cc = _compressor()
        states = _replay(cc, REAL_RUNAWAY_BOUNDARIES[:4])
        assert states[-1][3], (
            f"must trip by the second no-progress boundary; states={states}"
        )

    def test_real_runaway_does_not_trip_on_the_healthy_prefix(self):
        """Boundaries 1-2 reclaimed 60.7% and 5.7% -- real work, no trip."""
        cc = _compressor()
        states = _replay(cc, REAL_RUNAWAY_BOUNDARIES[:2])
        assert not states[-1][3], (
            f"a session reclaiming 60% and 5.7% must not be paused; states={states}"
        )

    def test_progress_resets_the_streak(self):
        """Boundary 8 reclaimed 11.6% -- that must reset the streak."""
        cc = _compressor()
        _replay(cc, REAL_RUNAWAY_BOUNDARIES[:8])
        assert cc._usage_less_loop_count == 0, (
            "a boundary that reclaimed 11.6% must reset the streak"
        )

    def test_under_threshold_clears_the_streak(self):
        """Boundary 19 lands under the threshold -- the streak must clear."""
        cc = _compressor()
        _replay(cc, REAL_RUNAWAY_BOUNDARIES[:19])
        assert cc._usage_less_loop_count == 0

    def test_under_threshold_never_trips(self):
        """A session that is under the line is never paused, however flat it is."""
        cc = _compressor()
        for _ in range(6):
            cc._last_compression_savings_pct = 0.0
            cc._record_usage_less_loop_verdict(50_000)
        assert not cc._usage_less_loop_tripped()


class TestDiscriminator:
    def test_flat_and_over_threshold_trips(self):
        """The loop signature: still over the line AND reclaiming nothing."""
        cc = _compressor()
        for _ in range(3):
            cc._last_compression_savings_pct = 0.0
            cc._record_usage_less_loop_verdict(500_000)
        assert cc._usage_less_loop_tripped()

    def test_one_no_progress_boundary_is_not_enough(self):
        """One bad round is not a loop; the second is.

        ``_USAGE_LESS_LOOP_TRIP_COUNT`` is 2: the first no-progress boundary only
        records a strike, and it takes a second consecutive one to pause compaction.
        """
        cc = _compressor()
        cc._last_compression_savings_pct = 0.0
        cc._record_usage_less_loop_verdict(500_000)
        assert not cc._usage_less_loop_tripped(), (
            "the first no-progress boundary must not pause compaction"
        )
        cc._last_compression_savings_pct = 0.0
        cc._record_usage_less_loop_verdict(500_000)
        assert cc._usage_less_loop_tripped(), (
            "two consecutive no-progress boundaries must pause automatic compaction"
        )

    def test_progress_resets_between_two_bad_rounds(self):
        """Progress between two bad rounds clears the streak.

        Sequence: bad, bad (trips), good (resets), bad, bad (trips again). The
        reset must genuinely clear the streak -- the trip after the reset must be
        earned by a FRESH pair of no-progress boundaries, not inherited.
        """
        cc = _compressor()
        for est, savings in ((500_000, 0.0), (500_000, 0.0), (500_000, 30.0)):
            cc._last_compression_savings_pct = savings
            cc._record_usage_less_loop_verdict(est)
        assert cc._usage_less_loop_count == 0, (
            "a reclaiming compaction must reset the streak to zero"
        )
        # A single fresh no-progress boundary after the reset must NOT trip.
        cc._last_compression_savings_pct = 0.0
        cc._record_usage_less_loop_verdict(500_000)
        assert not cc._usage_less_loop_tripped(), (
            "after a reset, one fresh no-progress boundary must not trip"
        )

    def test_large_but_progressing_session_is_not_paused(self):
        """The false-positive this detector was rewritten to avoid.

        A session 11x over the threshold whose compactions still reclaim tokens is
        doing real work. Pausing it would be a regression, not a fix.
        """
        cc = _compressor()
        for _ in range(8):
            cc._last_compression_savings_pct = 11.6
            cc._record_usage_less_loop_verdict(1_169_496)
        assert not cc._usage_less_loop_tripped(), (
            "a large but progressing session must never be paused"
        )

    def test_marginal_savings_is_treated_as_no_progress(self):
        """Savings below the threshold count as NO progress.

        The real runaway's marginal phase (boundaries 9-15) reclaimed 0.5%-1.3% --
        a rounding error against a 1.13M estimate. That is the loop's signature, not
        progress, and the threshold of 2% deliberately classifies it as such. The
        clearly-productive boundaries in the same session were 5.7%, 11.6%, 47.7%
        and 78.2%.
        """
        cc = _compressor()
        for _ in range(6):
            cc._last_compression_savings_pct = 1.3
            cc._record_usage_less_loop_verdict(1_139_132)
        assert cc._usage_less_loop_tripped(), (
            "1.3% reclaim against a 1.14M estimate is the loop signature"
        )

    def test_savings_above_the_threshold_counts_as_progress(self):
        """5.7% -- the real session's boundary 2 -- is genuine progress."""
        cc = _compressor()
        for _ in range(6):
            cc._last_compression_savings_pct = 5.7
            cc._record_usage_less_loop_verdict(1_329_152)
        assert not cc._usage_less_loop_tripped(), (
            "5.7% reclaim is real progress and must never be paused"
        )


class TestPauseIsBounded:
    def test_pause_expires(self):
        """The pause is bounded, not a permanent disarm."""
        cc = _compressor()
        for _ in range(3):
            cc._last_compression_savings_pct = 0.0
            cc._record_usage_less_loop_verdict(500_000)
        assert cc._usage_less_loop_tripped()
        cc._usage_less_loop_blocked_until = time.monotonic() - 1.0
        assert not cc._usage_less_loop_tripped(), (
            "the pause must expire on its own -- it must not permanently disarm compaction"
        )

    def test_pause_does_not_touch_the_provider_verdict_counter(self):
        """The usage-less verdict must NOT poison the permanent breaker.

        ``_ineffective_compression_count`` tripping is a PERMANENT disarm of
        auto-compaction. The usage-less verdict is a heuristic and keeps its own
        counters.
        """
        cc = _compressor()
        for _ in range(6):
            cc._last_compression_savings_pct = 0.0
            cc._record_usage_less_loop_verdict(500_000)
        assert cc._ineffective_compression_count == 0, (
            "the usage-less detector must not strike the permanent breaker"
        )


class TestProviderGate:
    def test_skipped_when_provider_reports_usage(self):
        """With real usage available, the existing authoritative path owns the case."""
        cc = _compressor()
        cc._provider_omits_usage = False
        cc.last_prompt_tokens = 100_000
        cc.awaiting_real_usage_after_compression = False
        cc.last_compression_rough_tokens = 500_000
        cc._maybe_record_usage_less_loop_verdict()
        assert cc._usage_less_loop_count == 0, (
            "must not double-judge when the provider reports usage"
        )

    def test_runs_when_provider_omits_usage(self):
        cc = _compressor()
        cc._provider_omits_usage = True
        cc.last_prompt_tokens = 0
        cc.awaiting_real_usage_after_compression = True
        cc.last_compression_rough_tokens = 500_000
        cc._last_compression_savings_pct = 0.0
        cc._maybe_record_usage_less_loop_verdict()
        assert cc._usage_less_loop_count == 1

    def test_rough_estimate_missing_is_a_safe_no_op(self):
        """No rough estimate -> no verdict, and crucially no exception."""
        cc = _compressor()
        cc._provider_omits_usage = True
        cc.last_compression_rough_tokens = 0
        cc._maybe_record_usage_less_loop_verdict()
        assert cc._usage_less_loop_count == 0
        assert not cc._usage_less_loop_tripped()


class TestGateInteraction:
    def test_automatic_compression_is_blocked_while_paused(self):
        cc = _compressor()
        for _ in range(3):
            cc._last_compression_savings_pct = 0.0
            cc._record_usage_less_loop_verdict(500_000)
        assert cc._automatic_compression_blocked_locally() is True

    def test_provider_proven_overflow_still_compacts(self):
        """``ignore_cooldown=True`` is the provider-PROVEN overflow path.

        The provider has already rejected the request for exceeding its context;
        blocking there would fail the turn. The pause must not apply there.
        """
        cc = _compressor()
        for _ in range(3):
            cc._last_compression_savings_pct = 0.0
            cc._record_usage_less_loop_verdict(500_000)
        assert cc._automatic_compression_blocked_locally(ignore_cooldown=True) is False, (
            "the provider-proven overflow path must still be allowed to compact"
        )

    def test_block_reason_is_reported(self):
        cc = _compressor()
        for _ in range(3):
            cc._last_compression_savings_pct = 0.0
            cc._record_usage_less_loop_verdict(500_000)
        assert cc._compression_block_reason() == "usage_less_loop"
