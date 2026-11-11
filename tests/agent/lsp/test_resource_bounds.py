"""Regression tests for the LSP resource bounds (kanban t_67049b90).

The language server was the UNBOUNDED consumer on the homelab host:

* four live ``pyright-langserver`` processes held 909 MiB of RSS, one observed
  growing 559 -> 666 MiB in 8 seconds;
* every one of them was charged to the GATEWAY's cgroup (``hermes-gateway.service``),
  not to a worker scope -- language servers are gateway children;
* that cgroup sat at 99.84% of ``memory.high`` with 49841 throttle events and
  only ~600 MiB of headroom to ``MemoryMax``.

One more server at 400 MiB would have taken the gateway past ``MemoryMax`` -- a
cgroup OOM kill of the entire messaging control plane.

Two bounds close that:

* ``lsp.max_server_heap_mb`` -- the V8 heap ceiling for node-based servers;
* ``lsp.max_clients`` -- the maximum number of LIVE language-server processes.

Both are RESOURCE bounds. Neither relaxes workspace trust: an untrusted checkout
still gets no project code loaded.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from agent.lsp.manager import UNTRUSTED_SHARED_ROOT, LSPService, _client_key, _int_or
from agent.lsp.servers import SERVERS, ServerContext, ServerDef, UNTRUSTED_SAFE_SERVERS, apply_node_heap_cap


def _pyright() -> ServerDef:
    """The pyright registry entry (the server that actually leaked)."""
    return cast(ServerDef, next(s for s in SERVERS if s.server_id == "pyright"))


class TestUntrustedRootsShareOneClient:
    """The ROOT CAUSE of the gateway cgroup accumulation (kanban t_2dfa12f1).

    A kanban worker has no operator anchor — ``agent.lsp.workspace.operator_workspace_roots``
    returns an empty set when ``HERMES_KANBAN_TASK`` is set — so every root a worker touches is
    UNTRUSTED. The old key shape keyed untrusted roots per-root, so every distinct checkout spawned
    its OWN pyright process, all of them children of the GATEWAY (the messaging control plane).
    Four of them held ~1 GiB RSS plus ~1 GiB swap and pinned the gateway at 99.9% of
    ``memory.high``; the reclaim storm starved the event loop and the gateway's own liveness
    watchdog killed it.

    The fix shares ONE untrusted multi-root process across all untrusted roots. It is safe because
    the trust gate still decides what the server may DO: an untrusted pyright is pinned to the
    operator's own interpreter, never the checkout's ``.venv``, so it never loads project code.
    """

    def test_pyright_is_multi_root(self):
        assert _pyright().multi_root is True

    def test_two_untrusted_roots_share_one_key(self):
        """THE regression: two untrusted roots must NOT mean two processes."""
        srv = _pyright()
        a = _client_key(srv, "/home/u/repo-a", trusted=False)
        b = _client_key(srv, "/home/u/repo-b", trusted=False)
        assert a == b, f"untrusted roots must share one client, got {a} and {b}"

    def test_the_shared_untrusted_key_is_distinct_from_the_trusted_key(self):
        """A trusted client is pinned to the first root's project venv — it may load project code,
        so it must never be shared with an untrusted root."""
        srv = _pyright()
        assert _client_key(srv, "/repo", trusted=True) == ("pyright", "")
        assert _client_key(srv, "/repo", trusted=False) == ("pyright", UNTRUSTED_SHARED_ROOT)
        assert _client_key(srv, "/repo", trusted=True) != _client_key(srv, "/repo", trusted=False)

    def test_the_shared_key_is_not_a_plausible_filesystem_root(self):
        """The sentinel must not collide with a real resolved project root."""
        assert UNTRUSTED_SHARED_ROOT not in ("", "/", ".", "..")
        assert not UNTRUSTED_SHARED_ROOT.startswith("/")

    def test_a_server_that_may_not_run_untrusted_still_gets_a_per_root_key(self):
        """The sharing applies ONLY to servers that are safe to run untrusted."""
        # Every multi-root server in the registry that is NOT safe to run untrusted must keep
        # its per-root key — the shared key would let it serve roots it may not serve.
        for other in SERVERS:
            if other.multi_root and other.server_id not in UNTRUSTED_SAFE_SERVERS:
                assert _client_key(other, "/repo-a", trusted=False) == (other.server_id, "/repo-a")
                assert _client_key(other, "/repo-b", trusted=False) == (other.server_id, "/repo-b")

    def test_a_non_multi_root_server_keeps_its_per_root_key(self):
        """tsserver-style per-root servers are unaffected by the shared key."""
        found = next((s for s in SERVERS if s.server_id == "typescript"), None)
        if found is None:
            pytest.skip("typescript server not in the registry")
        srv = cast(ServerDef, found)
        was_multi_root = srv.multi_root
        srv.multi_root = False
        try:
            assert _client_key(srv, "/repo-a", trusted=False) == ("typescript", "/repo-a")
            assert _client_key(srv, "/repo-b", trusted=False) == ("typescript", "/repo-b")
        finally:
            srv.multi_root = was_multi_root

    def test_trusted_roots_still_share_one_key(self):
        """The pre-existing trusted sharing is unchanged."""
        srv = _pyright()
        assert _client_key(srv, "/repo-a", trusted=True) == _client_key(srv, "/repo-b", trusted=True)


class TestLiveKeyReusesTheSharedUntrustedClient:
    """``_live_key`` must FIND the shared untrusted client instead of spawning a second one."""

    def _service(self) -> LSPService:
        svc = LSPService(
            enabled=True, wait_mode="document", wait_timeout=1.0,
            install_strategy="manual", idle_timeout=0, max_clients=2,
        )
        return svc

    def test_an_existing_shared_client_is_reused_for_another_untrusted_root(self):
        svc = self._service()
        try:
            shared = ("pyright", UNTRUSTED_SHARED_ROOT)
            svc._clients[shared] = cast(Any, object())
            svc._operator_roots = frozenset()
            svc._trusted_workspaces = []
            key = svc._live_key(_pyright(), "/some/other/untrusted/root")
            assert key == shared
        finally:
            svc._loop.stop()

    def test_a_live_per_root_client_is_still_found(self):
        """A client spawned for a specific root keeps being found after the root becomes trusted."""
        svc = self._service()
        try:
            own = ("pyright", "/repo")
            svc._clients[own] = cast(Any, object())
            svc._operator_roots = frozenset()
            svc._trusted_workspaces = []
            key = svc._live_key(_pyright(), "/repo")
            assert key == own
        finally:
            svc._loop.stop()


class TestNodeHeapCap:
    """``lsp.max_server_heap_mb`` -> ``NODE_OPTIONS=--max-old-space-size=N``."""

    def test_cap_is_applied(self):
        env = apply_node_heap_cap({}, 512)
        assert env["NODE_OPTIONS"] == "--max-old-space-size=512"

    def test_zero_means_no_cap(self):
        env = apply_node_heap_cap({}, 0)
        assert "NODE_OPTIONS" not in env

    def test_negative_means_no_cap(self):
        env = apply_node_heap_cap({}, -1)
        assert "NODE_OPTIONS" not in env

    def test_malformed_value_means_no_cap(self):
        env = apply_node_heap_cap({}, "not-a-number")
        assert "NODE_OPTIONS" not in env

    def test_existing_node_options_are_preserved_and_appended_to(self):
        """Other NODE_OPTIONS flags (e.g. --require) must survive."""
        env = apply_node_heap_cap({"NODE_OPTIONS": "--require /opt/hook.js"}, 512)
        assert "--require" in env["NODE_OPTIONS"]
        assert "--max-old-space-size=512" in env["NODE_OPTIONS"]

    def test_user_set_heap_cap_wins(self):
        """An operator's explicit ``--max-old-space-size`` is NOT overridden."""
        env = apply_node_heap_cap({"NODE_OPTIONS": "--max-old-space-size=2048"}, 512)
        assert env["NODE_OPTIONS"] == "--max-old-space-size=2048"
        assert "512" not in env["NODE_OPTIONS"]

    def test_cap_reaches_the_spawn_spec(self, monkeypatch):
        """The bound must survive the whole path into the spawned server's env."""
        import agent.lsp.servers as servers_mod

        # ``env_for`` reads the pm payload manifest from disk; stub it so the test
        # exercises the LSP code, not the package manager's install layout.
        monkeypatch.setattr(servers_mod, "env_for", lambda *a, **k: {}, raising=False)
        import pm

        monkeypatch.setattr(pm, "env_for", lambda *a, **k: {}, raising=False)

        ctx = ServerContext(workspace_root="/tmp", max_server_heap_mb=256)
        spec = servers_mod._make_spec("/tmp", ctx, "pyright", ["/bin/true"])
        assert "--max-old-space-size=256" in spec.env.get("NODE_OPTIONS", "")


class TestClientCap:
    """``lsp.max_clients`` -- LRU eviction of live language-server processes."""

    def _service(self, max_clients: int) -> LSPService:
        svc = LSPService(
            enabled=True, wait_mode="document", wait_timeout=1.0,
            install_strategy="manual", idle_timeout=0, max_clients=max_clients,
        )
        return svc

    class _FakeClient:
        """Minimal stand-in for :class:`LSPClient` — the eviction path reads
        ``server_id``/``workspace_root`` and calls ``shutdown()``."""

        def __init__(self, root: str = "/root"):
            self.server_id = "pyright"
            self.workspace_root = root
            self.shut = False

        async def shutdown(self):
            self.shut = True

    def _install(self, svc: LSPService, clients: dict) -> None:
        """Seed ``svc._clients`` with the fakes (the real type is LSPClient)."""
        svc._clients.update(cast(Any, clients))

    def test_zero_means_unlimited(self):
        svc = self._service(0)
        assert svc._max_clients == 0

    def test_negative_is_clamped_to_zero(self):
        svc = self._service(-5)
        assert svc._max_clients == 0

    def test_under_the_cap_nothing_is_evicted(self):
        svc = self._service(3)
        self._install(svc, {
            ("pyright", "/a"): self._FakeClient("/a"),
            ("pyright", "/b"): self._FakeClient("/b"),
        })
        svc._loop.run(svc._enforce_client_cap(), timeout=5.0)
        assert len(svc._clients) == 2
        svc._loop.stop()

    def test_over_the_cap_evicts_the_least_recently_used(self):
        svc = self._service(1)
        self._install(svc, {
            ("pyright", "/old"): self._FakeClient("/old"),
            ("pyright", "/new"): self._FakeClient("/new"),
        })
        svc._last_used = {("pyright", "/old"): 100.0, ("pyright", "/new"): 200.0}
        svc._loop.run(svc._enforce_client_cap(), timeout=5.0)
        assert ("pyright", "/old") not in svc._clients
        assert ("pyright", "/new") in svc._clients
        svc._loop.stop()

    def test_eviction_removes_the_recency_entry_too(self):
        svc = self._service(1)
        self._install(svc, {
            ("pyright", "/old"): self._FakeClient("/old"),
            ("pyright", "/new"): self._FakeClient("/new"),
        })
        svc._last_used = {("pyright", "/old"): 100.0, ("pyright", "/new"): 200.0}
        svc._loop.run(svc._enforce_client_cap(), timeout=5.0)
        assert ("pyright", "/old") not in svc._last_used
        svc._loop.stop()

    def test_evicted_client_is_shut_down(self):
        """The PROCESS must be killed, not just de-registered."""
        svc = self._service(1)
        old, new = self._FakeClient("/old"), self._FakeClient("/new")
        self._install(svc, {("pyright", "/old"): old, ("pyright", "/new"): new})
        svc._last_used = {("pyright", "/old"): 100.0, ("pyright", "/new"): 200.0}
        svc._loop.run(svc._enforce_client_cap(), timeout=5.0)
        assert old.shut is True
        assert new.shut is False
        svc._loop.stop()


class TestIntOr:
    """``_int_or`` must not let a malformed config value disable a bound."""

    def test_plain_int(self):
        assert _int_or(4, 0) == 4

    def test_none_falls_back(self):
        assert _int_or(None, 7) == 7

    def test_malformed_falls_back(self):
        assert _int_or("nope", 7) == 7

    def test_bool_falls_back(self):
        """``True`` is not a client count."""
        assert _int_or(True, 7) == 7

    def test_float_is_truncated(self):
        assert _int_or(2.9, 0) == 2


class TestConfigSurface:
    """The bounds must be reachable from config and default to ON."""

    def test_defaults_enable_both_bounds(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        lsp = DEFAULT_CONFIG["lsp"]
        assert lsp["max_server_heap_mb"] > 0, "the heap cap must default to ON"
        assert lsp["max_clients"] > 0, "the client cap must default to ON"

    def test_create_from_config_reads_the_bounds(self, monkeypatch):
        from agent.lsp import manager as mgr

        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"lsp": {"enabled": True, "max_server_heap_mb": 256, "max_clients": 3}},
        )
        svc = mgr.LSPService.create_from_config()
        assert svc is not None
        assert svc._max_server_heap_mb == 256
        assert svc._max_clients == 3
        svc._loop.stop()
