"""Regression tests for the LIVE worker-scope memory budget (t_67049b90).

The invariant the host must never break:

    (number of live worker scopes) x (per-worker MemoryMax)  <=  0.7 x MemTotal

The arithmetic invariant (``cap x limit <= budget``) was already proven for the
KANBAN scopes by ``test_memory_budget.py``.  What it could NOT see was the
scopes the cap does NOT count:

* cron executions create a scope the same way, draw the same limit, and their
  concurrency is NOT bounded by the Kanban cap;
* local managed workers do the same.

So the sum of live scope limits could exceed the host budget while the
Kanban-side arithmetic still read ``consistent``.  On the homelab host
(2026-11-11) the live population was 8 scopes x 1400 MiB = 11200 MiB against
an 11204 MiB budget — a 4 MiB margin that a single extra scope erased.

These tests pin the fix:

* ``count_live_worker_scopes()`` counts the scopes that actually claim memory;
* the per-worker limit is derived from that population PLUS the scope being
  created, so ``N scopes x limit <= budget`` holds for EVERY N;
* the registry actually passes the population through.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli import memory_budget as mb

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


def _budget(host: int) -> int:
    return int(host * mb.MEMORY_BUDGET_FRACTION)


class TestCountLiveWorkerScopes:
    def test_returns_none_when_the_cgroup_tree_is_unreadable(self, monkeypatch):
        monkeypatch.setattr(mb, "_worker_scope_dir", lambda: None)
        assert mb.count_live_worker_scopes() is None

    def test_returns_none_when_the_scope_dir_is_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mb, "_worker_scope_dir", lambda: tmp_path / "nope")
        assert mb.count_live_worker_scopes() is None

    def test_counts_only_scopes_that_claim_memory(self, monkeypatch, tmp_path):
        """An unlimited scope (``memory.max`` == ``max``) claims nothing."""
        scope_dir = tmp_path / "app.slice"
        scope_dir.mkdir()

        def make(name: str, limit: str) -> None:
            d = scope_dir / name
            d.mkdir()
            (d / "memory.max").write_text(limit, encoding="utf-8")

        make("hermes-worker-kanban-t_1.scope", str(1468006400))
        make("hermes-worker-cron-abc.scope", str(1468006400))
        make("hermes-worker-local-def.scope", str(1468006400))
        make("hermes-worker-unlimited.scope", "max")   # claims nothing
        make("some-other-unit.scope", str(1468006400))  # not a worker scope
        (scope_dir / "not-a-scope").mkdir()

        monkeypatch.setattr(mb, "_worker_scope_dir", lambda: scope_dir)
        assert mb.count_live_worker_scopes() == 3

    def test_counts_across_every_scope_kind(self, monkeypatch, tmp_path):
        """Cron and local scopes draw the same limit, so they count too."""
        scope_dir = tmp_path / "app.slice"
        scope_dir.mkdir()
        for name in (
            "hermes-worker-kanban-t_1.scope",
            "hermes-worker-kanban-t_2.scope",
            "hermes-worker-cron-abc.scope",
            "hermes-worker-local-def.scope",
        ):
            d = scope_dir / name
            d.mkdir()
            (d / "memory.max").write_text(str(1468006400), encoding="utf-8")
        monkeypatch.setattr(mb, "_worker_scope_dir", lambda: scope_dir)
        assert mb.count_live_worker_scopes() == 4

    def test_live_host_has_a_readable_population(self):
        """The live host must expose a countable scope population."""
        count = mb.count_live_worker_scopes()
        if count is None:
            pytest.skip("no cgroup v2 scope tree on this host")
        assert count >= 0


class TestPopulationSumFitsTheBudget:
    """THE invariant: N live scopes x their limit never exceeds the budget."""

    @pytest.mark.parametrize("population", range(1, 25))
    def test_every_population_sums_inside_the_budget(self, population):
        """For every N, the limit the Nth scope receives keeps the sum inside.

        This is the regression the card asked for: the old code derived the
        limit from the scopes ALREADY live, so the Nth scope was created with
        the (N-1)-population limit and ``N x limit`` overshot the budget.
        """
        host = 16 * GIB
        budget = _budget(host)
        limit = mb.per_worker_memory_max_bytes(host, live_scopes=population)
        assert limit is not None
        assert population * limit <= budget, (
            f"{population} scopes x {limit // MIB} MiB exceeds the "
            f"{budget // MIB} MiB budget"
        )

    def test_the_old_behaviour_overshot(self):
        """Pin WHY the +1 exists: counting only the live scopes overshoots."""
        host = 16 * GIB
        budget = _budget(host)
        # The 9th scope, created while 8 are live, using the 8-scope limit.
        limit = mb.per_worker_memory_max_bytes(host, live_scopes=8)
        assert limit is not None
        assert 9 * limit > budget, "the old derivation is expected to overshoot"

    def test_the_new_behaviour_does_not_overshoot(self):
        host = 16 * GIB
        budget = _budget(host)
        # The 9th scope counts ITSELF, so the limit tightens to fit.
        limit = mb.per_worker_memory_max_bytes(host, live_scopes=9)
        assert limit is not None
        assert 9 * limit <= budget

    def test_the_limit_tightens_as_the_population_grows(self):
        host = 16 * GIB
        few = mb.per_worker_memory_max_bytes(host, live_scopes=4)
        many = mb.per_worker_memory_max_bytes(host, live_scopes=24)
        assert few is not None and many is not None
        assert many < few

    def test_the_sum_never_exceeds_the_budget_on_a_small_host(self):
        host = 4 * GIB
        budget = _budget(host)
        for population in range(1, 20):
            limit = mb.per_worker_memory_max_bytes(host, live_scopes=population)
            assert limit is not None
            assert population * limit <= budget


class TestRegistryPassesThePopulation:
    def test_the_registry_counts_the_scope_being_created(self, monkeypatch):
        """The limit is read BEFORE the scope exists, so it must count itself."""
        import tools.process_registry as pr

        monkeypatch.setattr(pr._memory_budget, "count_live_worker_scopes", lambda: 8)
        assert pr._live_scope_count_including_this_one() == 9

    def test_the_registry_keeps_the_old_behaviour_when_unmeasurable(self, monkeypatch):
        import tools.process_registry as pr

        monkeypatch.setattr(pr._memory_budget, "count_live_worker_scopes", lambda: None)
        assert pr._live_scope_count_including_this_one() is None

    def test_the_registry_limit_tightens_with_the_population(self, monkeypatch):
        """The production path must actually use the population."""
        import tools.process_registry as pr

        host = mb.host_memory_total_bytes()
        if host is None:
            pytest.skip("host memory unknown")

        limits: dict[int, int] = {}
        for population in (2, 12):
            monkeypatch.setattr(
                pr._memory_budget, "count_live_worker_scopes", lambda p=population: p
            )
            limits[population] = pr._worker_memory_max_bytes()
        assert limits[12] < limits[2], (
            "the per-worker limit did not tighten with the live scope population"
        )

    def test_the_registry_limit_keeps_the_sum_inside_the_budget(self, monkeypatch):
        """The end-to-end invariant, through the production function."""
        import tools.process_registry as pr

        host = mb.host_memory_total_bytes()
        if host is None:
            pytest.skip("host memory unknown")
        budget = _budget(host)

        for population in range(1, 20):
            # ``population`` scopes already live; the registry creates one more.
            monkeypatch.setattr(
                pr._memory_budget, "count_live_worker_scopes", lambda p=population: p
            )
            limit = pr._worker_memory_max_bytes()
            total = (population + 1) * limit
            assert total <= budget, (
                f"{population + 1} scopes x {limit // MIB} MiB exceeds the "
                f"{budget // MIB} MiB budget"
            )


class TestBothSidesStillAgree:
    def test_the_registry_and_the_dispatcher_use_the_same_module(self):
        import tools.process_registry as pr
        from hermes_cli import kanban_db_dispatch as dispatch

        assert pr._memory_budget is mb
        assert dispatch.MEMORY_GUARD_MB_PER_WORKER == mb.MEMORY_GUARD_MB_PER_WORKER

    def test_the_live_host_satisfies_the_invariant(self):
        """The LIVE host, with the LIVE scope population, must fit."""
        host = mb.host_memory_total_bytes()
        if host is None:
            pytest.skip("host memory unknown")
        population = mb.count_live_worker_scopes()
        if population is None:
            pytest.skip("no cgroup v2 scope tree on this host")
        budget = _budget(host)
        limit = mb.per_worker_memory_max_bytes(host, live_scopes=population)
        assert limit is not None
        assert population * limit <= budget
