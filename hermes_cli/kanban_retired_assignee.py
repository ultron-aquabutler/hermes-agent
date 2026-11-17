"""Retired-assignee guard: a dead profile name must fail loudly, not silently.

The defect this closes (kanban card ``t_252663de``): the Hermes profile
``vision`` was retired on 2026-10-05, but the label survived as a kanban
assignee. The dispatcher buckets an assignee whose profile does not exist
into ``skipped_nonspawnable`` and keeps the card in ``ready`` — silently,
forever. The CLI even calls that state "terminal lane, OK". Six weeks later
22 cards were stranded that way, and cards were still being created against
the dead label.

Two distinct failure classes were being conflated:

1. **A control-plane lane** (e.g. ``orion-cc``, a Claude Code terminal that
   pulls cards via ``claim_task``). Expected steady-state; not actionable.
2. **A retired / dead profile name** (``vision``). Nobody will ever claim
   these cards. The card is stranded and the operator does not know.

The guard separates them with an explicit operator-declared registry,
``kanban.retired_assignees``. A name in that registry is NOT a lane: the
dispatcher must not silently bucket it. Instead it surfaces the card:

- a distinct ``assignee_retired`` board event naming the profile,
- a WARNING log line (visible in the gateway journal), and
- the card is parked ``blocked`` with ``block_kind='needs_input'`` so it
  shows up on the board as needing a human instead of sitting in ``ready``.

Creation is refused up front: ``kanban_create`` against a retired assignee
raises, so the card never exists to be stranded.

An assignee that is simply absent from disk and NOT in the registry keeps
the historical lane behaviour — the registry is opt-in, so an existing
multi-lane install is unaffected.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "RetiredAssigneeError",
    "is_retired_assignee",
    "retired_assignees",
]


class RetiredAssigneeError(ValueError):
    """Raised when a card is created against a retired assignee.

    A ``ValueError`` subclass so callers that already funnel assignee
    validation errors (``create_task`` raises ``ValueError`` for a bad
    profile name) keep working unchanged.
    """


def _config_kanban_section() -> dict:
    """The user's ``kanban:`` config section, without the DEFAULT_CONFIG merge.

    ``config_effective`` merges DEFAULT_CONFIG, whose ``None`` placeholder
    for ``retired_assignees`` would make the key look present in every home
    (the same trap ``kanban.dispatch_profiles`` documents). A read that
    raises yields an empty section — fail closed on the guard, never on the
    operator's registry.
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        # NOT fail_closed: a torn YAML edit must fall back to the last
        # known-good config rather than silently disabling the operator's
        # registry (that would reopen the exact silent-drop hole).
        cfg = load_user_config_effective() or {}
    except Exception:
        return {}
    section = cfg.get("kanban")
    return section if isinstance(section, dict) else {}


def retired_assignees() -> frozenset[str]:
    """Lowercased profile names the operator has explicitly retired.

    Config key ``kanban.retired_assignees`` — a list or a comma-separated
    string. Unset (the default) means the guard is inert and the historical
    lane behaviour is preserved everywhere.
    """
    raw = _config_kanban_section().get("retired_assignees")
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        names: Iterable[str] = (part.strip() for part in raw.split(","))
    elif isinstance(raw, (list, tuple, set)):
        names = (str(part).strip() for part in raw)
    else:
        return frozenset()
    out = set()
    for name in names:
        if not name:
            continue
        try:
            from hermes_cli.profiles import normalize_profile_name
            out.add(normalize_profile_name(name))
        except Exception:
            out.add(name.lower())
    return frozenset(out)


def is_retired_assignee(name: Optional[str]) -> bool:
    """True when *name* is an explicitly retired assignee."""
    if not name:
        return False
    registry = retired_assignees()
    if not registry:
        return False
    try:
        from hermes_cli.profiles import normalize_profile_name
        canon = normalize_profile_name(name)
    except Exception:
        canon = str(name).strip().lower()
    return canon in registry
