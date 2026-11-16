"""Tests for hermes_cli.deploy_guard — gate that fails loud when the running
tree is not on the canonical deploy branch (t_7161d9a9).

These tests do not depend on the live git tree; they synthesize a tempdir
with a custom ``HERMES_AGENT_HOME`` (and a fake ``.git``) so the assertions
exercise each branch / reason in isolation. The live-tree end-to-end check
is part of the deployed-tree acceptance drive (see kanban-block-loop-quarantine
acceptance in t_7161d9a9).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.deploy_guard import (
    DEFAULT_DEPLOY_BRANCH,
    DeployBranchDriftError,
    check_deploy_branch,
    check_deploy_state,
)


def _make_git_repo(tmp_path: Path, branch: str) -> Path:
    """Initialize a fresh git repo at tmp_path and leave it on ``branch``."""
    env = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@y", "GIT_COMMITTER_NAME": "x",
           "GIT_COMMITTER_EMAIL": "x@y"}
    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "x@y"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "x"], check=True, env=env)
    (tmp_path / "README").write_text("hi")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README"], check=True, env=env)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True, env=env)
    return tmp_path


def test_match_when_on_canonical_branch(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path, "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_DEPLOY_GUARD", "warn")
    r = check_deploy_state()
    assert r.ok is True
    assert r.reason == "match"
    assert r.expected_branch == "canonical-deploy-t_7161d9a9"
    assert r.observed_branch == "canonical-deploy-t_7161d9a9"
    assert r.observed_sha and len(r.observed_sha) >= 7


def test_wrong_branch_returns_reason_wrong_branch(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path, "fix/some-feature")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_DEPLOY_GUARD", "warn")
    r = check_deploy_state()
    assert r.ok is False
    assert r.reason == "wrong_branch"
    assert r.expected_branch == "canonical-deploy-t_7161d9a9"
    assert r.observed_branch == "fix/some-feature"


def test_detached_head_returns_reason_detached(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path, "main")
    # Detach HEAD at the current commit.
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "--detach", sha], check=True)
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "main")
    r = check_deploy_state()
    assert r.ok is False
    assert r.reason == "detached"
    assert r.observed_branch is None
    assert r.observed_sha == sha


def test_missing_repo_returns_reason_missing_repo(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("HERMES_AGENT_HOME", str(empty))
    r = check_deploy_state()
    assert r.ok is False
    assert r.reason == "missing_repo"


def test_env_override_changes_expected_branch(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path, "main")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "main")
    r = check_deploy_state()
    assert r.ok is True
    assert r.expected_branch == "main"


def test_default_branch_is_canonical_deploy(monkeypatch):
    """Without HERMES_DEPLOY_BRANCH the default targets the canonical deploy
    branch. Future promotions to ``main`` should bump the default AND update
    this test in the same commit.
    """
    monkeypatch.delenv("HERMES_DEPLOY_BRANCH", raising=False)
    assert DEFAULT_DEPLOY_BRANCH == "canonical-deploy"


def test_strict_mode_raises_on_drift(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path, "fix/feature")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_DEPLOY_GUARD", "warn")
    with pytest.raises(DeployBranchDriftError) as ei:
        check_deploy_branch(strict=True)
    assert "fix/feature" in str(ei.value)
    assert "canonical-deploy-t_7161d9a9" in str(ei.value)


def test_strict_mode_does_not_raise_on_match(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path, "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
    r = check_deploy_branch(strict=True)
    assert r.ok is True


class TestStrictModeRaisesOnDriftOnly:
    """t_59318511: strict must refuse a tick on DRIFT, not on INDETERMINATE.

    ``missing_repo`` / ``git_unavailable`` mean the guard could not READ the
    tree, not that the tree is on the wrong branch. Strict used to raise on
    both, which refused dispatch for every profile-scoped process: a profile
    home (``~/.hermes/profiles/<name>``) has no ``hermes-agent/`` child, so
    the check reported ``missing_repo`` and the tick died with rc=1.
    """

    def test_strict_does_not_raise_on_missing_repo(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("HERMES_AGENT_HOME", str(empty))
        monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
        monkeypatch.setenv("HERMES_DEPLOY_GUARD", "strict")
        r = check_deploy_branch()
        assert r.reason == "missing_repo"
        assert r.ok is False

    def test_strict_does_not_raise_for_profile_scoped_home(self, tmp_path, monkeypatch):
        """A profile HERMES_HOME must not refuse the tick under strict."""
        profile_home = tmp_path / "profiles" / "curator"
        profile_home.mkdir(parents=True)
        monkeypatch.delenv("HERMES_AGENT_HOME", raising=False)
        monkeypatch.delenv("HERMES_AGENT", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
        monkeypatch.setenv("HERMES_DEPLOY_GUARD", "strict")
        r = check_deploy_branch()
        assert r.reason == "missing_repo"
        assert r.agent_home == str(profile_home / "hermes-agent")

    def test_strict_still_raises_on_wrong_branch(self, tmp_path, monkeypatch):
        """The guard stays enforced: a drifted tree still refuses the tick."""
        repo = _make_git_repo(tmp_path, "fix/feature")
        monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
        monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
        monkeypatch.setenv("HERMES_DEPLOY_GUARD", "strict")
        with pytest.raises(DeployBranchDriftError):
            check_deploy_branch()

    def test_strict_still_raises_on_detached(self, tmp_path, monkeypatch):
        """Detached HEAD is drift too — a worker checked out a feature branch."""
        repo = _make_git_repo(tmp_path, "main")
        sha = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", sha], check=True)
        monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
        monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "main")
        monkeypatch.setenv("HERMES_DEPLOY_GUARD", "strict")
        with pytest.raises(DeployBranchDriftError):
            check_deploy_branch()

    def test_missing_repo_logs_at_error_with_agent_home(self, tmp_path, monkeypatch, caplog):
        """The skipped check stays LOUD: ERROR, and it names the resolved home."""
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("HERMES_AGENT_HOME", str(empty))
        monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
        monkeypatch.setenv("HERMES_DEPLOY_GUARD", "strict")
        with caplog.at_level("ERROR", logger="hermes_cli.deploy_guard"):
            check_deploy_branch()
        errors = [rec for rec in caplog.records if rec.levelname == "ERROR"]
        assert errors, "missing_repo must be logged at ERROR so a misresolved home stays visible"
        assert str(empty) in errors[0].message

    def test_drift_logs_at_warning_not_error(self, tmp_path, monkeypatch, caplog):
        """Drift keeps its WARNING level — the drift banner is a warning."""
        repo = _make_git_repo(tmp_path, "fix/feature")
        monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
        monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
        monkeypatch.setenv("HERMES_DEPLOY_GUARD", "strict")
        with caplog.at_level("WARNING", logger="hermes_cli.deploy_guard"):
            with pytest.raises(DeployBranchDriftError):
                check_deploy_branch()
        assert not [rec for rec in caplog.records if rec.levelname == "ERROR"]
        assert [rec for rec in caplog.records if rec.levelname == "WARNING"]

    def test_agent_home_env_overrides_profile_home(self, tmp_path, monkeypatch):
        """The pinned deployment rung wins over a profile-scoped HERMES_HOME.

        This is the (2) half: the deployment sites pin HERMES_AGENT_HOME so a
        profile-scoped process inspects the REAL deployed tree — the guard is
        enforced, not skipped.
        """
        repo = _make_git_repo(tmp_path, "canonical-deploy-t_7161d9a9")
        profile_home = tmp_path / "profiles" / "curator"
        profile_home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
        r = check_deploy_state()
        assert r.ok is True
        assert r.agent_home == str(repo)


def test_off_mode_returns_synthetic_match(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path, "fix/anything")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_DEPLOY_GUARD", "off")
    r = check_deploy_branch()
    assert r.ok is True
    assert r.reason == "match"  # synthetic, not a real inspection


def test_warn_mode_returns_real_result_on_drift(tmp_path, monkeypatch, caplog):
    repo = _make_git_repo(tmp_path, "fix/leak")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_DEPLOY_GUARD", "warn")
    with caplog.at_level("WARNING", logger="hermes_cli.deploy_guard"):
        r = check_deploy_branch()
    assert r.ok is False
    assert r.reason == "wrong_branch"
    assert any("DRIFT" in rec.message for rec in caplog.records)


def test_strict_override_wins_over_env(tmp_path, monkeypatch):
    """``strict=False`` passed as kwarg downgrades a strict env var to warn."""
    repo = _make_git_repo(tmp_path, "fix/leak")
    monkeypatch.setenv("HERMES_AGENT_HOME", str(repo))
    monkeypatch.setenv("HERMES_DEPLOY_BRANCH", "canonical-deploy-t_7161d9a9")
    monkeypatch.setenv("HERMES_DEPLOY_GUARD", "strict")
    r = check_deploy_branch(strict=False)
    assert r.ok is False  # still detected
    # No raise — kwarg downgraded the env var.


class TestDispatchOnceGuardFailsLoud:
    """AC5: the deploy guard must be able to REFUSE a tick on a drifted tree.

    ``dispatch_once`` used to wrap the whole guard call in
    ``except Exception: pass``, which silently swallowed
    ``DeployBranchDriftError`` -- so ``HERMES_DEPLOY_GUARD=strict`` could
    never actually block anything. These tests pin the fail-loud contract:
    a drift raises out of ``dispatch_once``, while a broken guard *import*
    still does not block a tick.

    The assertion is on the GUARD, not on downstream DB work: a dummy
    connection is used so the test measures the guard contract alone and
    does not depend on board schema.
    """

    def _dispatch_once(self, monkeypatch, guard_module):
        import sys

        import hermes_cli.kanban_db_dispatch as kbd

        class _Conn:
            """Minimal stand-in: the guard runs before any DB access."""

            def execute(self, *a, **kw):
                raise AssertionError("guard did not stop the tick before DB access")

            def close(self):
                pass

        monkeypatch.setitem(sys.modules, "hermes_cli.deploy_guard", guard_module)
        return kbd.dispatch_once(_Conn())

    def test_strict_drift_propagates_out_of_dispatch_once(self, monkeypatch):
        from hermes_cli.deploy_guard import DeployBranchDriftError

        guard = type("m", (), {})()
        guard.check_deploy_branch = staticmethod(
            lambda *a, **kw: (_ for _ in ()).throw(DeployBranchDriftError("drifted"))
        )

        with pytest.raises(DeployBranchDriftError):
            self._dispatch_once(monkeypatch, guard)

    def test_broken_guard_import_does_not_block_a_tick(self, monkeypatch):
        import builtins
        import sys

        real_import = builtins.__import__

        def _fake_import(name, *a, **kw):
            if name == "hermes_cli.deploy_guard":
                raise ImportError("simulated broken guard")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        monkeypatch.setitem(sys.modules, "hermes_cli.deploy_guard", None)

        # The guard import failure must NOT raise. The dispatch may still fail
        # later on the dummy connection -- that is fine; the point is that the
        # guard import failure itself did not abort the tick.
        try:
            self._dispatch_once(monkeypatch, None)
        except AssertionError:
            # Reached DB access -> the tick was NOT blocked by the guard import.
            pass
