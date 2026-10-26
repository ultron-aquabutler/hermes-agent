"""A worktree whose own ``pm`` predates the borrow fix still boots, on borrowed DATA.

The code-level borrow (a worktree's own ``pm`` resolving its owner's committed
generation) only helps a worktree that carries that fix. A worktree cut before it
carries a ``pm`` that has never heard of borrowing: it resolves its own install key,
finds no ``facts.json``, and PM's store Python -- which owns no dependencies --
refuses the process outright ("no dependency environment is committed for this
install"), so the worker dies before it does any work (t_boot_defect_619cbb).

Writing the SAME borrow as DATA reaches those worktrees: the worktree's install
state points at the owner's generation, and an older ``pm`` reads it like any other
selection record.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pm.environments import (
    borrowed_install_owner,
    committed_venv,
    install_state_dir,
    provision_borrowed_install_state,
    runtime_facts_path,
    site_packages,
)

# Every lane: the borrow is read off the filesystem (a ``.git`` file, a symlinked
# ``environments`` directory) and nothing in it is OS-specific.
pytestmark = pytest.mark.platforms("any")


def _checkout(root: Path) -> Path:
    """A main checkout that looks like Hermes (it owns the shared ``.git``)."""
    (root / "hermes_cli").mkdir(parents=True)
    (root / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    (root / ".git").mkdir()
    return root


def _worktree(root: Path, owner: Path, name: str = "wt") -> Path:
    """A linked worktree of *owner*: ``.git`` is a FILE pointing into the shared gitdir."""
    worktree = root / ".worktrees" / name
    worktree.mkdir(parents=True)
    (worktree / "hermes_cli").mkdir()
    (worktree / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    gitdir = owner / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return worktree


def _commit_generation(owner: Path) -> Path:
    """Give *owner* a committed generation and the facts record that names it."""
    generation = install_state_dir(owner) / "environments" / "gen" / "venv"
    site = site_packages(generation)
    site.mkdir(parents=True)
    (generation / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    runtime_facts_path(owner).parent.mkdir(parents=True, exist_ok=True)
    runtime_facts_path(owner).write_text(json.dumps({
        "schema": 1,
        "packages": {"venv": {"environment": str(generation), "stamp": "verified"}},
    }), encoding="utf-8")
    return generation


def test_a_worktree_without_state_is_given_the_owners_generation(tmp_path):
    owner = _checkout(tmp_path / "hermes-agent")
    worktree = _worktree(tmp_path / "hermes-agent", owner)
    generation = _commit_generation(owner)

    assert provision_borrowed_install_state(worktree) == install_state_dir(worktree)

    # The borrow resolves to the owner's generation -- the same tree, the same
    # interpreter, the same dependencies.
    assert committed_venv(worktree) == generation.resolve()
    assert borrowed_install_owner(worktree) == owner.resolve()


def test_a_borrowed_worktree_reads_its_state_the_way_an_older_pm_does(tmp_path):
    """The point of the DATA-level borrow: a selection record an older ``pm`` accepts.

    ``_recorded_venv`` resolves the recorded environment and requires it to sit under
    ``<install>/environments``. The symlinked ``environments`` directory is what makes
    that hold for a record naming the owner's generation.
    """
    owner = _checkout(tmp_path / "hermes-agent")
    worktree = _worktree(tmp_path / "hermes-agent", owner)
    generation = _commit_generation(owner)
    provision_borrowed_install_state(worktree)

    record = json.loads(runtime_facts_path(worktree).read_text(encoding="utf-8"))
    environment = Path(record["packages"]["venv"]["environment"]).resolve()
    generations = (install_state_dir(worktree) / "environments").resolve()
    assert environment.is_relative_to(generations)
    assert (environment / "pyvenv.cfg").is_file()
    assert environment == generation.resolve()


def test_provisioning_is_idempotent(tmp_path):
    owner = _checkout(tmp_path / "hermes-agent")
    worktree = _worktree(tmp_path / "hermes-agent", owner)
    _commit_generation(owner)

    assert provision_borrowed_install_state(worktree) is not None
    first = runtime_facts_path(worktree).read_text(encoding="utf-8")
    assert provision_borrowed_install_state(worktree) is None  # it already has state
    assert runtime_facts_path(worktree).read_text(encoding="utf-8") == first


def test_a_worktree_with_its_own_state_is_never_borrowed_over(tmp_path):
    """A worktree that committed a generation of its own keeps it."""
    owner = _checkout(tmp_path / "hermes-agent")
    worktree = _worktree(tmp_path / "hermes-agent", owner)
    _commit_generation(owner)
    own = install_state_dir(worktree) / "environments" / "own" / "venv"
    site = site_packages(own)
    site.mkdir(parents=True)
    (own / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    runtime_facts_path(worktree).parent.mkdir(parents=True, exist_ok=True)
    runtime_facts_path(worktree).write_text(json.dumps({
        "schema": 1, "packages": {"venv": {"environment": str(own), "stamp": "verified"}},
    }), encoding="utf-8")

    assert provision_borrowed_install_state(worktree) is None
    assert committed_venv(worktree) == own.resolve()


def test_a_checkout_that_is_not_a_worktree_borrows_nothing(tmp_path):
    owner = _checkout(tmp_path / "hermes-agent")
    _commit_generation(owner)

    assert provision_borrowed_install_state(owner) is None
    assert borrowed_install_owner(owner) is None


def test_a_worktree_of_an_owner_with_nothing_committed_borrows_nothing(tmp_path):
    owner = _checkout(tmp_path / "hermes-agent")
    worktree = _worktree(tmp_path / "hermes-agent", owner)

    assert provision_borrowed_install_state(worktree) is None
    assert not runtime_facts_path(worktree).is_file()


def test_a_real_environments_directory_is_never_replaced(tmp_path):
    """The borrow symlinks ``environments``; a real directory there belongs to this
    install and must survive."""
    owner = _checkout(tmp_path / "hermes-agent")
    worktree = _worktree(tmp_path / "hermes-agent", owner)
    _commit_generation(owner)
    environments = install_state_dir(worktree) / "environments"
    environments.mkdir(parents=True)
    (environments / "own-generation").mkdir()

    assert provision_borrowed_install_state(worktree) is None
    assert (environments / "own-generation").is_dir()
    assert not environments.is_symlink()


def test_a_borrowed_install_refuses_to_build_into_the_owners_tree(tmp_path):
    """A dependency sync in a borrowed checkout would write a generation into the
    OWNER's tree -- and the owner's collector would later delete it as unreferenced.
    The sync is refused, and the owner is named."""
    from pm.packages import InstallError, Venv

    owner = _checkout(tmp_path / "hermes-agent")
    worktree = _worktree(tmp_path / "hermes-agent", owner)
    _commit_generation(owner)
    provision_borrowed_install_state(worktree)

    package = Venv.__new__(Venv)
    package.name = "venv"
    package.project_root = lambda: worktree

    with pytest.raises(InstallError) as excinfo:
        package.apply([], explicit=True)
    assert str(owner) in str(excinfo.value)


def test_an_install_of_its_own_still_builds(tmp_path):
    """The guard is about borrows only: an install with its own tree is unaffected."""
    owner = _checkout(tmp_path / "hermes-agent")
    _commit_generation(owner)

    assert borrowed_install_owner(owner) is None
    assert borrowed_install_owner(_checkout(tmp_path / "other")) is None

