"""A card's skill-preload list names skills the assignee's profile may not carry.

The preload list travels with the CARD, but skills resolve against the ACTIVE
profile's own skills dir (``tools/skills_tool.py:_skills_dir``). A card that
names one profile's skill names it for every assignee, so a missing skill used
to kill the worker BEFORE its first turn -- the owner settled the admission
``unknown``, the submitter exited rc=1, and the dispatcher re-claimed the card
forever (t_b6107c56: 92 identical deaths, zero productive iterations).

The gateway's own session path and the CLI's ``hermes -s`` both treat a
missing skill as non-fatal. This pins the kanban worker path to the same
contract, and keeps the SAFE-WORKER refusal intact.
"""
import pytest


def _context(skills):
    return {'task_id': 't_test', 'skills': skills, 'context': '', }


def test_missing_skill_does_not_kill_the_worker(monkeypatch):
    """A skill this profile does not carry is skipped, not fatal."""
    from gateway import session_kanban

    monkeypatch.setattr(
        'agent.skill_commands.build_preloaded_skills_prompt',
        lambda identifiers, task_id=None: (
            'preloaded body',
            ['present-skill'],
            ['swarm-service-deployment', 'infra-state-verification'],
        ),
    )
    monkeypatch.setattr('agent.safe_worker_policy.safe_worker_enabled', lambda: False)

    prompt = session_kanban._preload_task_skills(
        _context(['swarm-service-deployment', 'infra-state-verification']), 'session-1')

    assert 'preloaded body' in prompt
    assert 'swarm-service-deployment' in prompt
    assert 'infra-state-verification' in prompt
    # The model is told which names did not resolve, and that they are advisory.
    assert 'could not be preloaded' in prompt
    assert 'advisory context' in prompt


def test_all_skills_resolve_returns_the_prompt_unchanged(monkeypatch):
    """No missing skill means no notice is appended."""
    from gateway import session_kanban

    monkeypatch.setattr(
        'agent.skill_commands.build_preloaded_skills_prompt',
        lambda identifiers, task_id=None: ('preloaded body', ['a', 'b'], []),
    )
    monkeypatch.setattr('agent.safe_worker_policy.safe_worker_enabled', lambda: False)

    prompt = session_kanban._preload_task_skills(_context(['a', 'b']), 'session-1')

    assert prompt == 'preloaded body'
    assert 'could not be preloaded' not in prompt


def test_safe_worker_policy_still_refuses(monkeypatch):
    """A SAFE-WORKER policy withholds skills on purpose; that refusal stands."""
    from gateway import session_kanban

    monkeypatch.setattr(
        'agent.skill_commands.build_preloaded_skills_prompt',
        lambda identifiers, task_id=None: ('', [], list(identifiers)),
    )
    monkeypatch.setattr('agent.safe_worker_policy.safe_worker_enabled', lambda: True)

    with pytest.raises(ValueError, match='kanban_skills_unavailable'):
        session_kanban._preload_task_skills(_context(['withheld-skill']), 'session-1')


def test_run_task_turns_survives_an_unloadable_card_skill(monkeypatch):
    """End to end through the turn path: a missing card skill must not raise.

    This is the shape that killed t_b6107c56's worker: the card named two
    skills the librarian profile does not carry, the preload raised, and the
    worker died before it could reach ``kanban_complete``.
    """
    from gateway import session_kanban

    monkeypatch.setattr(
        'agent.skill_commands.build_preloaded_skills_prompt',
        lambda identifiers, task_id=None: ('', [], ['infra-state-verification']),
    )
    monkeypatch.setattr('agent.safe_worker_policy.safe_worker_enabled', lambda: False)

    seen = {}

    class _Agent:
        session_id = 'session-1'

        def run_conversation(self, prompt, conversation_history=None, turn_author=None):
            seen['prompt'] = prompt
            return {'messages': []}

    agent = _Agent()
    context = dict(_context(['infra-state-verification']),
                   context='card body', run_id=7, db='/tmp/none', claim_lock='lock-1',
                   goal_mode=False)
    frame = {'text': 'do the work'}

    session_kanban._run_task_turns(agent, frame, [], context)

    assert 'card body' in seen['prompt']
    assert 'do the work' in seen['prompt']
    assert 'could not be preloaded' in seen['prompt']
