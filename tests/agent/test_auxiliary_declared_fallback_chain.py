"""The auxiliary recovery ladder honours the DECLARED ``fallback_providers`` chain
for an EXPLICIT-provider task (t_498b52ce).

Live incident: on 2026-11-23 the main provider (minimax) was unavailable and the
friday gateway's kanban dispatcher spawned 0 workers for ~2h45m while the ready
queue stayed non-empty. The gateway logged::

    Auxiliary kanban_decomposer: main provider minimax is unavailable and no
    fallback_chain / fallback_providers is configured — refusing to guess
    another logged-in provider.

but the friday profile config plainly declared::

    fallback_providers:
      - provider: copilot
        model: gpt-6-luna

Two things were wrong:

1. the ladder refused an EXPLICIT-provider task outright on an auth failure and
   never consulted the DECLARED chain at all (the ladder's
   ``_ladder_provider_fallback`` only reached the main chain when ``is_auto``);
2. the refusal message claimed no ``fallback_providers`` was configured, which
   sent triage down the "the key is spelled wrong" path instead of the real
   cause.

A DECLARED chain is an operator-declared route — using it is not "guessing
another logged-in provider". The DISCOVERY chain (whatever else happens to be
logged in) must still be refused, and the main-agent-model rung must still be
excluded for an explicit pin.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent import auxiliary_client as ac
from agent.auxiliary_client import call_llm


class _AuxAuth401(Exception):
    """Auth-flavoured provider failure (what an outage looks like)."""


class _DummyResponse:
    def __init__(self, text: str):
        self.choices = [MagicMock()]
        self.choices[0].message.content = text
        self.choices[0].message.tool_calls = None


@pytest.fixture(autouse=True)
def _fresh_unhealthy_cache():
    ac._reset_aux_unhealthy_cache()
    yield
    ac._reset_aux_unhealthy_cache()


def _auth_error() -> Exception:
    exc = _AuxAuth401("HTTP 401 unauthorized: invalid api key")
    exc.status_code = 401
    return exc


def test_declared_main_chain_is_consulted_for_explicit_provider_auth_failure():
    """An explicit-provider task with a DECLARED ``fallback_providers`` chain
    uses that chain after an auth failure instead of refusing outright."""
    primary = MagicMock()
    primary.base_url = "https://api.minimax.example"
    primary.chat.completions.create.side_effect = _auth_error()

    declared = MagicMock()
    declared.base_url = "https://api.individual.githubcopilot.com"
    declared.chat.completions.create.return_value = _DummyResponse("declared-chain-serves")

    with patch("agent.auxiliary_client._resolve_task_provider_model",
               return_value=("custom:colibri", None, None, None, None)), \
         patch("agent.auxiliary_client._get_cached_client",
               return_value=(primary, "primary-model")), \
         patch("agent.auxiliary_client._try_configured_fallback_chain",
               return_value=(None, None, "")), \
         patch("agent.auxiliary_client._declared_main_fallback_chain",
               return_value=[{"provider": "copilot", "model": "gpt-6-luna"}]), \
         patch("agent.auxiliary_client._try_main_fallback_chain",
               return_value=(declared, "gpt-6-luna", "fallback_providers[0](copilot)")) as mock_main_chain, \
         patch("agent.auxiliary_client._try_payment_fallback",
               return_value=(None, None, "")), \
         patch("agent.auxiliary_client._try_main_agent_model_fallback",
               return_value=(None, None, "")) as mock_agent_model:
        result = call_llm(
            task="kanban_decomposer",
            messages=[{"role": "user", "content": "decompose"}],
        )

    assert result.choices[0].message.content == "declared-chain-serves"
    # The DECLARED chain was walked...
    assert mock_main_chain.call_count >= 1
    # ...but the main-agent-model rung stayed excluded for the explicit pin.
    assert mock_agent_model.call_count == 0


def test_explicit_provider_auth_failure_without_any_declared_chain_still_refuses():
    """With NO declared chain, the explicit-provider boundary still holds.

    The ladder must refuse rather than invent a route — the discovery chain is
    deliberately NOT reachable here (it would bill an account the operator
    never pointed this session at).
    """
    primary = MagicMock()
    primary.base_url = "https://api.minimax.example"
    primary.chat.completions.create.side_effect = _auth_error()

    with patch("agent.auxiliary_client._resolve_task_provider_model",
               return_value=("custom:colibri", None, None, None, None)), \
         patch("agent.auxiliary_client._get_cached_client",
               return_value=(primary, "primary-model")), \
         patch("agent.auxiliary_client._try_configured_fallback_chain",
               return_value=(None, None, "")), \
         patch("agent.auxiliary_client._declared_main_fallback_chain",
               return_value=[]), \
         patch("agent.auxiliary_client._try_main_fallback_chain",
               return_value=(None, None, "")) as mock_main_chain, \
         patch("agent.auxiliary_client._try_main_agent_model_fallback",
               return_value=(None, None, "")):
        with pytest.raises(Exception):
            call_llm(
                task="kanban_decomposer",
                messages=[{"role": "user", "content": "decompose"}],
            )

    # Nothing was declared, so the main chain was never walked.
    assert mock_main_chain.call_count == 0


def test_refusal_message_does_not_claim_no_fallback_providers_when_declared(caplog):
    """The refusal text must not deny a declared chain that exists.

    The 2026-11-23 message ("no fallback_chain / fallback_providers is
    configured") was read as a key-name mismatch when the real cause was that
    the ladder never consulted the declared chain.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="agent.auxiliary_client"):
        with patch("agent.auxiliary_client._declared_main_fallback_chain",
                   return_value=[{"provider": "copilot", "model": "gpt-6-luna"}]):
            assert ac._discovery_chain_allowed("minimax", "kanban_decomposer") is False

    text = caplog.text
    assert "no fallback_chain / fallback_providers is configured" not in text
    assert "fallback_providers" in text


def test_refusal_message_still_states_nothing_declared_when_empty(caplog):
    """When nothing IS declared, the original wording is kept."""
    import logging

    with caplog.at_level(logging.WARNING, logger="agent.auxiliary_client"):
        with patch("agent.auxiliary_client._declared_main_fallback_chain",
                   return_value=[]):
            assert ac._discovery_chain_allowed("minimax", "kanban_decomposer") is False

    assert "no fallback_chain / fallback_providers is configured" in caplog.text


def test_declared_chain_reaches_the_main_chain_rung_not_just_the_task_chain():
    """The DECLARED chain must actually be WALKED, not merely unblocked.

    This is the behavioural core of the t_498b52ce fix: on the unpatched tree
    the ladder returned early for an explicit-provider auth failure unless the
    TASK had its own ``fallback_chain``, so a declared top-level
    ``fallback_providers`` chain was never consulted and the request died.
    """
    from agent import auxiliary_error_policy as aux_errors

    declared_client = MagicMock()
    declared_client.base_url = "https://api.individual.githubcopilot.com"
    declared_client.chat.completions.create.return_value = _DummyResponse("declared-serves")

    route = ac._LadderRoute(
        client=MagicMock(),
        task="kanban_decomposer",
        tag="",
        async_mode=False,
        base_info="https://api.minimax.example",
        resolved_provider="custom:colibri",
        resolved_model="primary-model",
        resolved_base_url="https://api.minimax.example",
        resolved_api_key="k",
        resolved_api_mode=None,
        final_model="primary-model",
        main_runtime=None,
        route_info=None,
        timeout=None,
    )
    failure = aux_errors.Failure(
        error=_auth_error(),
        verdict=aux_errors.classify_attempt(_auth_error(), "custom:colibri"),
    )

    with patch("agent.auxiliary_client._declared_main_fallback_chain",
               return_value=[{"provider": "copilot", "model": "gpt-6-luna"}]), \
         patch("agent.auxiliary_client._try_configured_fallback_chain",
               return_value=(None, None, "")), \
         patch("agent.auxiliary_client._try_main_fallback_chain",
               return_value=(declared_client, "gpt-6-luna", "fallback_providers[0](copilot)")) as mock_main_chain:
        # ``_ladder_provider_fallback`` is a generator: the walk happens as it is
        # consumed, so the steps must be drained for the chain to be tried.
        steps = list(ac._ladder_provider_fallback(failure, route))

    # The declared chain was consulted, and its candidate is what the ladder
    # asks the driver to call. A ``_LadderStep`` of kind "fallback" carries
    # (client, model, label).
    assert mock_main_chain.call_count >= 1
    assert len(steps) == 1
    step = steps[0]
    assert step.kind == "fallback"
    assert step.args[0] is declared_client
    assert step.args[1] == "gpt-6-luna"
