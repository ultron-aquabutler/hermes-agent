"""Tests: a worktree worker is given the dependency environment its worktree lacks.

A worktree worker imports ``hermes_cli`` from its OWN worktree -- for ``python -m``,
``sys.path[0]`` is the cwd, and the cwd outranks the pinned ``PYTHONPATH``. The install
key hashes the checkout path, so a worktree is a distinct install with no
``facts.json`` of its own, and PM's store Python refuses the process: "no dependency
environment is committed for this install" (t_boot_defect_619cbb).

The dispatcher closes that by writing the borrow as DATA -- the worktree's install
state points at the owner's generation -- which reaches a worktree whose own ``pm``
predates the code-level borrow.
"""

from __future__ import annotations

import json
from pathlib import Path


def _make_task(kb, *, workspace_kind: str = "worktree"):
    return kb.Task(
        id="t_wt_state",
        title="worktree install state",
        body=None,
        assignee="w",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind=workspace_kind,
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


def _hermes_checkout(root: Path) -> Path:
    (root / "hermes_cli").mkdir(parents=True)
    (root / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    (root / ".git").mkdir()
    return root


def _linked_worktree(root: Path, owner: Path, name: str = "wt") -> Path:
    worktree = root / ".worktrees" / name
    worktree.mkdir(parents=True)
    (worktree / "hermes_cli").mkdir()
    (worktree / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    gitdir = owner / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return worktree


def _commit_generation(owner: Path, installs_root: Path) -> Path:
    """Give *owner* a committed generation under *installs_root*."""
    import hashlib

    key = hashlib.sha256(str(owner.resolve()).encode("utf-8")).hexdigest()[:16]
    state = installs_root / key
    generation = state / "environments" / "gen" / "venv"
    site = generation / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (generation / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    state.mkdir(parents=True, exist_ok=True)
    (state / "facts.json").write_text(json.dumps({
        "schema": 1,
        "packages": {"venv": {"environment": str(generation), "stamp": "verified"}},
    }), encoding="utf-8")
    return generation


def test_a_worktree_worker_is_given_the_owners_generation(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    home = tmp_path / ".hermes"
    (home / "profiles" / "w").mkdir(parents=True)
    (home / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    home.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    owner = _hermes_checkout(tmp_path / "hermes-agent")
    worktree = _linked_worktree(tmp_path / "hermes-agent", owner)
    generation = _commit_generation(owner, home / "installs")

    kbd._provision_worktree_install_state(_make_task(kb), str(worktree))

    # The worktree's install state names the owner's generation...
    from pm.environments import install_key, runtime_facts_path

    facts = runtime_facts_path(worktree)
    assert facts.is_file()
    record = json.loads(facts.read_text(encoding="utf-8"))
    assert Path(record["packages"]["venv"]["environment"]).resolve() == generation.resolve()
    # ...and the worktree's own install key is the one that was written.
    assert install_key(worktree) == facts.parent.name


def test_a_scratch_workspace_is_never_given_install_state(monkeypatch, tmp_path):
    """Only a worktree checkout needs the borrow; a scratch workspace has no checkout
    of its own, so nothing is written."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    home = tmp_path / ".hermes"
    (home / "profiles" / "w").mkdir(parents=True)
    (home / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    home.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    scratch = tmp_path / "scratch"
    scratch.mkdir()

    kbd._provision_worktree_install_state(_make_task(kb, workspace_kind="scratch"), str(scratch))

    installs = home / "installs"
    assert not installs.exists() or not any(installs.iterdir())
