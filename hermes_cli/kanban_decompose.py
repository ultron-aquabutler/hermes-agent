"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the gateway
dispatcher's auto-decompose path. Reads the profile roster (with
descriptions), asks the auxiliary LLM for a task graph in JSON, then
atomically creates the children, links them under the root, and flips the
root ``triage -> todo``. The root stays alive as parent of every leaf child so
it wakes back up when the graph completes and its assignee (the orchestrator
profile) can judge completion and add more work.

Mirrors ``kanban_specify`` (lazy aux import, lenient parse, never raises on
expected failures). ``fanout=false`` collapses to the ``specify`` behaviour
(tighten + promote, no children), making ``decompose`` a strict superset.
Unknown assignees are rewritten to ``default_assignee`` — a child NEVER ends
up with ``assignee=None``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import profiles as profiles_mod
from hermes_cli.kanban_specify import (
    _call_aux, _extract_json_blob, _load_triage_task, _task_prompt_fields, _title_body,
)
from hermes_cli.kanban_specify import _profile_author as _specify_author

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


# Quarantine gate (t_8b48a01f, restored 2026-09-23 by t_efc7769a): if
# ``block_task`` set ``hub_escalation=1`` because the unblock-loop breaker
# tripped, refuse to act on this task from either the CLI or the auto-
# decomposer. Direct CLI invocations of ``hermes kanban decompose`` /
# ``hermes kanban specify`` on a quarantined card are an operator footgun —
# they re-arm the structural loop the breaker was designed to break.
#
# Eligibility filter for the auto-decomposer (t_334f608b, restored 2026-09-23
# by t_efc7769a): skip tasks that are explicitly human-gated. Decomposing a
# "Bryan hand only" card does not produce agent-executable children — it
# produces one blocked card plus N children that block identically,
# regenerating the same wall each cycle.
#
# Both gates run BEFORE the auxiliary LLM call (verified by
# test_decompose_quarantined_card_does_not_call_aux) so a single bad
# retry never burns a turn of context-budget.
def _human_gate_match(
    task: kb.Task,
    comments: list | None = None,
) -> tuple[bool, str]:
    """``(matched, pattern)`` — True when *task* is human-gated and must be
    skipped by the decomposer/specifier. ``comments`` is the task's comment
    thread (passed in so we don't double-open the conn); when omitted,
    pattern 5 (status + vision_flag comment) cannot be evaluated.
    """
    if getattr(task, "hub_escalation", False):
        # Quarantine beat the human-gate title/body check: a quarantine is a
        # stronger block (loop-detected) than a title-prefix signal.
        return True, "quarantine:hub_escalation=1"
    title = (task.title or "").strip()
    body = task.body or ""
    lower_title = title.lower()
    # 1: title begins with literal "Bryan:" (case-insensitive prefix)
    if lower_title.startswith("bryan:"):
        return True, "title_prefix:Bryan:"
    # 2: title begins with literal "BRYAN HAND" (case-sensitive)
    if title.startswith("BRYAN HAND"):
        return True, "title_prefix:BRYAN HAND"
    # 3: body contains the literal phrase "BRYAN HAND ONLY"
    if "BRYAN HAND ONLY" in body:
        return True, "body_marker:BRYAN HAND ONLY"
    # 4: body contains "Bryan hand only" (case-insensitive)
    if "bryan hand only" in body.lower():
        return True, "body_marker:Bryan hand only"
    # 5: status in {blocked, triage, archived} AND a "default"-authored
    # comment carries the literal phrase "human-gated by design"
    if task.status in {"blocked", "triage", "archived"} and comments:
        for c in comments:
            if c.author == "default" and "human-gated by design" in (c.body or ""):
                return True, "vision_flag:human-gated by design"
    return False, ""


def _record_decomposer_skip(task_id: str, reason: str, **extra: object) -> None:
    """Best-effort append of a ``decomposer_skipped`` audit event.

    Silent refusal is a footgun (operators can't tell why a card didn't
    move); this writes to ``task_events`` so the skip shows up in any
    ``task_events`` view. Failure is logged at DEBUG and swallowed.
    """
    try:
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "decomposer_skipped",
                {"reason": reason, **extra},
            )
    except Exception as exc:
        logger.debug(
            "decompose: failed to record decomposer_skipped event on %s: %s",
            task_id, exc,
        )


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return _specify_author("decomposer")


def _resolve_profile_from_cfg(cfg: dict, key: str) -> str:
    """``kanban.<key>`` if it names an existing profile, else the active
    default profile — so a task is never stranded for lack of an owner.
    ``orchestrator_profile`` owns the root after fan-out; ``default_assignee``
    catches children the decomposer can't route."""
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get(key) or "").strip()
    if explicit:
        try:
            if profiles_mod.profile_exists(explicit):
                return explicit
        except Exception:
            pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """``(roster_for_prompt, valid_assignee_names)``; entries are
    ``{name, description, has_description}``."""
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return [], set()
    roster = []
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
    return roster, {p.name for p in all_profiles}


def _format_roster(roster: list[dict]) -> str:
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    return "\n".join(
        f"  - {entry['name']}{'' if entry['has_description'] else ' ⚠ undescribed'}: {entry['description']}"
        for entry in roster
    )


def _normalize_assignee_choice(assignee: object, *, default_assignee: str, valid_names: set[str]) -> str:
    """A valid assignee, else ``default_assignee`` — promoted work is never
    left unassigned."""
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    return chosen if chosen in valid_names else default_assignee


@dataclass
class _Routing:
    """Config-derived routing context for one decomposition."""

    orchestrator: str
    default_assignee: str
    auto_promote: bool
    roster: list[dict]
    valid_names: set[str]


def _load_routing() -> _Routing:
    from hermes_cli.config import load_config_readonly
    try:
        cfg = load_config_readonly()
    except Exception:  # decompose_task promises ok=False, never a raise, on config trouble
        cfg = {}
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    roster, valid_names = _build_roster()
    return _Routing(
        orchestrator=_resolve_profile_from_cfg(cfg, "orchestrator_profile"),
        default_assignee=_resolve_profile_from_cfg(cfg, "default_assignee"),
        auto_promote=bool(kanban_cfg.get("auto_promote_children", True)),
        roster=roster,
        valid_names=valid_names,
    )


def _apply_single(task: kb.Task, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    """``fanout=false``: single-task spec promotion (same effect as specify)."""
    title_val, body_val = _title_body(parsed)
    assignee_val = None
    if not task.assignee:
        assignee_val = _normalize_assignee_choice(
            parsed.get("assignee"), default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
    if title_val is None and body_val is None:
        return DecomposeOutcome(task.id, False, "decomposer returned fanout=false with no title/body")
    with kbc.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn, task.id, title=title_val, body=body_val, assignee=assignee_val, author=author,
        )
    if not ok:
        return DecomposeOutcome(task.id, False, "task moved out of triage before promotion")
    return DecomposeOutcome(task.id, True, "single task (no fanout)", fanout=False, new_title=title_val)


def _clean_children(task_id: str, raw_tasks: list, routing: _Routing) -> tuple[list[dict], str]:
    """Validate/normalise the LLM's ``tasks`` list; ``(children, "")`` or ``([], reason)``.
    Unknown assignees route to the default; never assignee=None."""
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return [], f"tasks[{idx}] is not an object"
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], f"tasks[{idx}].title is missing or empty"
        body = entry.get("body")
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee, default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
        if isinstance(assignee, str) and assignee.strip() and assignee.strip() not in routing.valid_names:
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, routing.default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        children.append({
            "title": title.strip()[:200],
            "body": body.strip() if isinstance(body, str) else "",
            "assignee": chosen,
            # Drop non-int, out-of-range and self parent indices.
            "parents": [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx],
        })
    return children, ""


def _apply_fanout(task_id: str, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(task_id, False, "decomposer returned fanout=true with empty tasks list")
    children, reason = _clean_children(task_id, raw_tasks, routing)
    if reason:
        return DecomposeOutcome(task_id, False, reason)
    try:
        with kbc.connect_closing() as conn:
            child_ids = decompose_triage_task(
                conn,
                task_id,
                root_assignee=routing.orchestrator,
                children=children,
                author=author,
                auto_promote=routing.auto_promote,
            )
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")
    if child_ids is None:
        return DecomposeOutcome(task_id, False, "task already decomposed or moved out of triage")
    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children", fanout=True, child_ids=child_ids,
    )


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks. Expected failures
    (not in triage, no aux client, API error, malformed/empty reply) surface
    as ``ok=False``."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, reason)

    # Eligibility gates (locked 2026-08-28, t_8b48a01f / t_334f608b;
    # restored 2026-09-23 by t_efc7769a). Both run BEFORE the auxiliary LLM
    # call so a bad retry never burns a turn of context. Comments are loaded
    # here so pattern 5 of the human-gate filter has the data it needs.
    try:
        with kbc.connect_closing() as conn:
            comments = kb.list_comments(conn, task_id)
    except Exception:
        comments = []
    matched, pattern = _human_gate_match(task, comments=comments)
    if matched:
        # Quarantine is a stronger skip than a title-marker (loop-detected
        # by the breaker, not just human-flagged). They share the same
        # skip-shape; the audit reason distinguishes them.
        reason = "quarantined" if pattern == "quarantine:hub_escalation=1" else "human_gate"
        if reason == "quarantined":
            logger.warning(
                "decompose: refusing %s — quarantined (hub_escalation=1, "
                "block_loop circuit breaker tripped). Clear with "
                "`UPDATE tasks SET hub_escalation=0 WHERE id='%s';` "
                "after the operator decides.",
                task_id, task_id,
            )
            _record_decomposer_skip(
                task_id, reason,
                hub_escalation=1, caller="decompose_task",
            )
            return DecomposeOutcome(
                task_id, False,
                "quarantined (hub_escalation=1); clear flag manually to resume",
            )
        # Plain human-gate skip (title prefix / body marker / vision flag).
        logger.info(
            "decompose: skipping %s — human-gated (%s)", task_id, pattern,
        )
        _record_decomposer_skip(
            task_id, reason, matched_pattern=pattern,
        )
        return DecomposeOutcome(
            task_id, False,
            f"human-gated by design ({pattern}); skipping decomposition",
        )

    routing = _load_routing()
    raw, reason = _call_aux(
        "decompose", task_id, aux_task="kanban_decomposer", system=_SYSTEM_PROMPT,
        user=_USER_TEMPLATE.format(
            **_task_prompt_fields(task),
            roster=_format_roster(routing.roster),
            default_assignee=routing.default_assignee,
        ),
        max_tokens=4000, timeout=timeout or 180, log=logger,
    )
    if raw is None:
        return DecomposeOutcome(task_id, False, reason)

    parsed = _extract_json_blob(raw, _FENCE_RE)
    if parsed is None:
        return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

    audit_author = author or _profile_author()
    if not parsed.get("fanout"):
        return _apply_single(task, parsed, routing, audit_author)
    return _apply_fanout(task_id, parsed, routing, audit_author)


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Return task ids currently in the triage column.

    Quarantine gate (t_8b48a01f, restored 2026-09-23 by t_efc7769a):
    drop ``hub_escalation=1`` rows. Re-introducing them to the
    auto-decompose sweep re-creates the structural loop
    ``block_loop_detected -> triage -> auto-specify -> ready -> worker
    re-blocks -> repeat every ~10s``. Operators un-quarantine by hand:
      UPDATE tasks SET hub_escalation=0 WHERE id='<task-id>';
    """
    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)
    # SQLite stores booleans as 0/1; the Task dataclass coerces to bool in
    # from_row, so ``hub_escalation=True`` on a quarantined card. The
    # ``getattr`` default protects against older DBs / future renames that
    # don't yet expose the column.
    return [
        row.id for row in rows
        if not getattr(row, "hub_escalation", False)
    ]


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
import json  # noqa: F401,E402
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
