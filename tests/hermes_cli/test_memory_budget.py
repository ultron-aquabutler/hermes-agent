"""Regression tests for the shared host memory budget (t_28090196).

The invariant both sides of the worker lifecycle must satisfy:

    effective_max_workers x per_worker_memory_max <= MEMORY_BUDGET_FRACTION x MemTotal

The two sides used to disagree:
  * ``tools.process_registry._worker_memory_max_bytes()`` picked a bound with no
    awareness of how many workers would run;
  * ``hermes_cli.kanban_db_dispatch.derive_default_max_in_progress()`` assumed
    512 MiB per worker while the measured footprint is ~1400 MiB.

On the homelab host (2026-10-28, card ``t_8e8ec4de``) that produced a 1.5x
overcommit at 3 workers and 4x at the derived cap of 8.

The derivation order is deliberate: the per-worker limit is PRIMARY (a worker
must never be too tight to run -- the ``t_4d12c8eb`` failure mode) and the
concurrency cap is DERIVED from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import memory_budget as mb

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


class TestHostMemory:
    def test_returns_none_when_unknown(self, monkeypatch):
        monkeypatch.setattr(
            mb.os, "sysconf", lambda name: (_ for _ in ()).throw(OSError("nope"))
        )
        assert mb.host_memory_total_bytes() is None

    def test_reads_real_host(self):
        total = mb.host_memory_total_bytes()
        assert total is None or total > 0


class TestPerWorkerLimit:
    def test_unknown_host_yields_none(self):
        assert mb.per_worker_memory_max_bytes(None) is None

    def test_zero_or_negative_host_yields_none(self):
        assert mb.per_worker_memory_max_bytes(0) is None
        assert mb.per_worker_memory_max_bytes(-1) is None

    def test_never_below_the_floor(self):
        # A tiny host must not produce a limit below the floor.
        limit = mb.per_worker_memory_max_bytes(64 * MIB)
        assert limit >= mb.MIN_WORKER_MEMORY_MAX_BYTES

    def test_never_above_the_ceiling(self):
        limit = mb.per_worker_memory_max_bytes(256 * GIB)
        assert limit <= mb.MAX_WORKER_MEMORY_MAX_BYTES

    def test_measured_footprint_is_the_binding_bound(self):
        """With no other constraint, the measured footprint wins.

        The old code assumed 512 MiB, which understated the real 1343-1375 MiB
        footprint by ~2.7x and produced an unachievable cap of 8.
        """
        host = 8 * GIB
        limit = mb.per_worker_memory_max_bytes(host)
        assert limit == mb.MEMORY_GUARD_MB_PER_WORKER * MIB

    def test_host_budget_tightens_the_limit(self):
        """A host with less RAM than the measured footprint must tighten it."""
        host = 2 * GIB
        limit = mb.per_worker_memory_max_bytes(host)
        assert limit < mb.MEMORY_GUARD_MB_PER_WORKER * MIB

    def test_worker_count_tightens_the_limit(self):
        """More workers -> smaller per-worker limit, so the sum still fits.

        Only binds when the host budget is TIGHTER than the measured
        footprint. On an 8 GiB host the budget per worker is 1853 MiB, which
        is above the 1400 MiB measured footprint, so the measured bound wins
        and the count does not tighten it. That is correct: the limit is
        already the smaller of the two.
        """
        host = 4 * GIB
        one = mb.per_worker_memory_max_bytes(host, 1)
        many = mb.per_worker_memory_max_bytes(host, 4)
        assert many <= one
        # And the invariant must hold for the tightened case.
        assert 4 * many <= int(host * mb.MEMORY_BUDGET_FRACTION)

    def test_count_bound_applies_when_it_is_tighter(self):
        """A small host where the budget per worker IS the binding constraint."""
        host = 3 * GIB
        one = mb.per_worker_memory_max_bytes(host, 1)
        many = mb.per_worker_memory_max_bytes(host, 4)
        assert many < one
        assert 4 * many <= int(host * mb.MEMORY_BUDGET_FRACTION)

    def test_live_scopes_tighten_the_limit(self):
        """Cron scopes draw the same per-worker limit and are unbounded in count."""
        host = 8 * GIB
        alone = mb.per_worker_memory_max_bytes(host)
        crowded = mb.per_worker_memory_max_bytes(host, live_scopes=8)
        assert crowded < alone

    def test_slice_limit_is_honored_when_usable(self):
        host = 8 * GIB
        slice_limit = 2 * GIB
        limit = mb.per_worker_memory_max_bytes(host, slice_limit=slice_limit)
        assert limit <= slice_limit

    def test_unusable_slice_limit_is_ignored_not_clamped(self):
        """A slice limit BELOW the floor is not a usable bound.

        Clamping it UP to the floor would silently turn "no usable bound"
        into a 64 MiB limit, which kills every worker.
        """
        host = 8 * GIB
        limit = mb.per_worker_memory_max_bytes(
            host, slice_limit=mb.MIN_WORKER_MEMORY_MAX_BYTES // 2
        )
        assert limit >= mb.MIN_WORKER_MEMORY_MAX_BYTES
        assert limit == mb.MEMORY_GUARD_MB_PER_WORKER * MIB


class TestEffectiveMaxWorkers:
    def test_unknown_host_yields_none(self):
        assert mb.effective_max_workers(None) is None

    def test_clamped_to_the_floor(self):
        # A host too small for even two workers still gets the floor of 2.
        assert mb.effective_max_workers(512 * MIB) >= mb.DERIVED_MAX_IN_PROGRESS_FLOOR

    def test_clamped_to_the_ceiling(self):
        # A huge host must not derive an unbounded fan-out.
        assert mb.effective_max_workers(1024 * GIB) <= mb.DERIVED_MAX_IN_PROGRESS_CEILING

    def test_derived_cap_matches_the_per_worker_limit(self):
        """The cap is floor(budget / per_worker_limit) -- not an independent guess."""
        host = 8 * GIB
        per_worker = mb.per_worker_memory_max_bytes(host)
        cap = mb.effective_max_workers(host)
        budget = int(host * mb.MEMORY_BUDGET_FRACTION)
        assert cap == max(
            mb.DERIVED_MAX_IN_PROGRESS_FLOOR,
            min(budget // per_worker, mb.DERIVED_MAX_IN_PROGRESS_CEILING),
        )

    def test_live_host_is_consistent(self):
        """The live host must satisfy the invariant with the live numbers."""
        host = mb.host_memory_total_bytes()
        if host is None:
            pytest.skip("host memory unknown")
        per_worker = mb.per_worker_memory_max_bytes(host)
        cap = mb.effective_max_workers(host)
        assert mb.budget_is_consistent(host, cap, per_worker)


class TestBudgetConsistency:
    def test_consistent_when_it_fits(self):
        host = 8 * GIB
        per_worker = mb.MEMORY_GUARD_MB_PER_WORKER * MIB
        assert mb.budget_is_consistent(host, 3, per_worker)

    def test_inconsistent_when_it_does_not_fit(self):
        host = 8 * GIB
        per_worker = mb.MEMORY_GUARD_MB_PER_WORKER * MIB
        assert not mb.budget_is_consistent(host, 8, per_worker)

    def test_unknown_host_is_never_consistent(self):
        assert not mb.budget_is_consistent(None, 3, 1024 * MIB)

    def test_zero_values_are_never_consistent(self):
        assert not mb.budget_is_consistent(8 * GIB, 0, 1024 * MIB)
        assert not mb.budget_is_consistent(8 * GIB, 3, 0)


class TestBothSidesAgree:
    """The two call sites must import the SAME module, not two copies of the idea."""

    def test_dispatch_uses_the_shared_module(self):
        from hermes_cli import kanban_db_dispatch as dispatch

        assert dispatch.MEMORY_GUARD_MB_PER_WORKER == mb.MEMORY_GUARD_MB_PER_WORKER
        assert (
            dispatch.DERIVED_MAX_IN_PROGRESS_FLOOR == mb.DERIVED_MAX_IN_PROGRESS_FLOOR
        )
        assert (
            dispatch.DERIVED_MAX_IN_PROGRESS_CEILING
            == mb.DERIVED_MAX_IN_PROGRESS_CEILING
        )

    def test_registry_uses_the_shared_module(self):
        import tools.process_registry as registry

        assert registry._memory_budget is mb

    def test_the_two_sides_agree_on_a_real_host(self, monkeypatch):
        """Derive the cap, then confirm the registry's limit fits the budget.

        NOTE: the test suite patches ``_system_memory_sample`` to an empty
        dict (tests/conftest.py) so results do not depend on the CI runner's
        live memory. This test opts out by feeding an explicit sample, which
        is the same seam the upstream tests use.
        """
        from hermes_cli import kanban_db_dispatch as dispatch
        import tools.process_registry as registry

        host = mb.host_memory_total_bytes()
        if host is None:
            pytest.skip("host memory unknown")
        registry_limit = registry._worker_memory_max_bytes()
        sample = {"mem_total_kib": host // 1024}
        cap = dispatch.derive_default_max_in_progress(sample)
        assert cap is not None
        # The registry limit and the derived cap must not overcommit the host.
        assert cap * registry_limit <= int(host * mb.MEMORY_BUDGET_FRACTION)

    def test_derive_returns_none_when_memory_is_unknown(self):
        """Unknown memory must fail open (no cap), not crash the dispatcher."""
        from hermes_cli.kanban_db_dispatch import derive_default_max_in_progress

        assert derive_default_max_in_progress({}) is None
        assert derive_default_max_in_progress({"mem_total_kib": 0}) is None
        assert derive_default_max_in_progress({"mem_total_kib": "nope"}) is None


class TestStaleGuessIsGone:
    def test_per_worker_assumption_is_not_512_mib(self):
        """512 MiB was the stale planning number that caused the overcommit."""
        assert mb.MEMORY_GUARD_MB_PER_WORKER != 512
        assert mb.MEMORY_GUARD_MB_PER_WORKER >= 1024
