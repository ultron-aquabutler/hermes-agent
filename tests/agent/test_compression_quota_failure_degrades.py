"""An auxiliary provider that runs out of quota must not be billed to the main model.

The auxiliary compression provider (for example ``copilot/gpt-6.1-terra``) and the main agent
model (for example ``minimax/MiniMax-M3``) are usually two ends of the SAME budget. When the
auxiliary provider returns a payment/quota error, the historical behaviour was:

1. ``_fallback_to_main_for_compression`` cleared ``summary_model`` and retried the summary on the
   main model — a direct charge on the protected provider for every compaction.
2. The cleared ``summary_model`` was never restored, so every later compaction in that session
   also went to the main model, even after the auxiliary provider recovered.
3. The same error set ``_last_summary_auth_failure``, which makes ``compress()`` ABORT. The
   session then grows past the threshold again and compacts again, repeating the cycle every few
   minutes.

``compression.quota_failure_degrades`` (off by default) changes that: the main model is not
charged, compression is not aborted, and the deterministic fallback summary is used instead. The
configured auxiliary model is restored once the provider answers again.
"""

from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor


def _compressor(**kwargs):
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="main-model", provider="main-provider", quiet_mode=True,
            base_url="https://main.example/v1", api_key="***",
            summary_model_override="quota-exhausted-aux",
            **kwargs,
        )


def _quota_error():
    """The exact shape the auxiliary client raises when a provider is out of credit."""
    error = Exception("payment error: quota exceeded")
    error.status_code = 402
    return error


def _access_error():
    error = Exception("Error code: 403 - model is not available in the current token plan")
    error.status_code = 403
    return error


def _ok_response(text="summary text"):
    ok = MagicMock()
    ok.choices = [MagicMock()]
    ok.choices[0].message.content = text
    return ok


# --------------------------------------------------------------------------------------
# The opt-out: default behaviour is unchanged.
# --------------------------------------------------------------------------------------


def test_default_still_falls_back_to_the_main_model():
    """Without the opt-in, the historical main-model retry must still happen."""
    compressor = _compressor()
    assert compressor.quota_failure_degrades is False

    with patch("agent.context_compressor.call_llm", side_effect=[_quota_error(), _ok_response()]) as calls:
        compressor._generate_summary([
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ])

    assert len(calls.call_args_list) == 2
    assert calls.call_args_list[1].kwargs.get("model") == "main-model"


# --------------------------------------------------------------------------------------
# The opt-in: no main-model charge.
# --------------------------------------------------------------------------------------


def test_quota_failure_does_not_charge_the_main_model():
    compressor = _compressor(quota_failure_degrades=True)

    with patch("agent.context_compressor.call_llm", side_effect=_quota_error()) as calls:
        result = compressor._generate_summary([
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ])

    # Exactly one call — the failing auxiliary one. No retry on the main model.
    assert len(calls.call_args_list) == 1
    assert calls.call_args_list[0].kwargs.get("model") == "quota-exhausted-aux"
    assert result is None


def test_quota_failure_does_not_abort_compression():
    """The failure must not set the terminal flag that makes compress() preserve the session."""
    compressor = _compressor(quota_failure_degrades=True)

    with patch("agent.context_compressor.call_llm", side_effect=_quota_error()):
        compressor._generate_summary([
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ])

    assert compressor._last_summary_auth_failure is False


def test_quota_failure_arms_a_cooldown():
    compressor = _compressor(quota_failure_degrades=True)

    with patch("agent.context_compressor.call_llm", side_effect=_quota_error()):
        compressor._generate_summary([
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ])

    assert compressor.get_active_compression_failure_cooldown(refresh=True) is not None


def test_access_error_also_degrades_when_opted_in():
    """A 403 on the auxiliary provider is the same leak: the main model is the same budget."""
    compressor = _compressor(quota_failure_degrades=True)

    with patch("agent.context_compressor.call_llm", side_effect=_access_error()) as calls:
        result = compressor._generate_summary([
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ])

    assert len(calls.call_args_list) == 1
    assert result is None
    assert compressor._last_summary_auth_failure is False


# --------------------------------------------------------------------------------------
# The sticky pin: the auxiliary model must come back after it recovers.
# --------------------------------------------------------------------------------------


def test_configured_summary_model_is_restored_after_a_recovery():
    """A temporary fallback must not pin the compressor to the main model for the session."""
    compressor = _compressor(quota_failure_degrades=True)
    assert compressor.summary_model == "quota-exhausted-aux"

    # Simulate the fallback path having cleared the model.
    compressor._fallback_to_main_for_compression(_quota_error(), "failed")
    assert compressor.summary_model == ""

    # The provider answers again.
    compressor._record_summary_success("a healthy summary")
    assert compressor.summary_model == "quota-exhausted-aux"
    assert compressor._summary_model_fallen_back is False


def test_restore_does_not_fire_when_no_model_was_configured():
    """A compressor with no auxiliary model must not gain one."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(
            model="main-model", provider="main-provider", quiet_mode=True,
            base_url="https://main.example/v1", api_key="***",
        )
    compressor._record_summary_success("a healthy summary")
    assert compressor.summary_model == ""


def test_restore_keeps_an_explicit_model_untouched():
    """A compressor already pointing somewhere must not be re-pointed."""
    compressor = _compressor(quota_failure_degrades=True)
    compressor.summary_model = "some-other-model"
    compressor._record_summary_success("a healthy summary")
    assert compressor.summary_model == "some-other-model"
