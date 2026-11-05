# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Prices for the current Claude models, and a warning for a model without one (#5771).

A model with no row in :data:`MODEL_PRICING` costs 0, so the spend limit
per login does not count runs on it. These tests check that:

* each current model has a price row, and a run on the default model
  costs more than 0;
* the default model is ``anthropic:claude-sonnet-5-5``;
* MEHO warns at start when the default model has no price row, and still
  starts;
* the status read says whether the spend of the default model is counted.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from meho_backplane import main as main_module
from meho_backplane.api.v1.health import _agent_model_spend
from meho_backplane.operations.identity_budget import (
    MODEL_PRICING,
    PerMillionPricing,
    TokenUsage,
    compute_cost,
)
from meho_backplane.operations.unpriced_model import is_model_priced, warn_if_model_unpriced
from meho_backplane.settings import Settings, get_settings

_UNPRICED_EVENT = "agent_default_model_unpriced"
_UNKNOWN_MODEL = "anthropic:claude-not-a-real-model"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env that ``Settings`` and the start of the app need."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", "https://meho.test")
    monkeypatch.delenv("AGENT_DEFAULT_MODEL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# Anthropic's published prices per million tokens on 2026-11-05:
# input, output, cache read, 5-minute cache write.
_CURRENT_MODEL_PRICES = {
    "anthropic:claude-opus-5-5": ("4.00", "20.00", "0.20", "5.00"),
    "anthropic:claude-sonnet-5-5": ("2.00", "10.00", "0.10", "2.50"),
    "anthropic:claude-haiku-5-5": ("0.10", "0.50", "0.01", "0.125"),
}


@pytest.mark.parametrize(("model_id", "prices"), sorted(_CURRENT_MODEL_PRICES.items()))
def test_each_current_model_has_its_price_row(model_id: str, prices: tuple[str, ...]) -> None:
    """Each current model has a row with the published prices."""
    input_, output, cache_read, cache_write = (Decimal(p) for p in prices)
    assert MODEL_PRICING[model_id] == PerMillionPricing(
        input=input_, output=output, cache_read=cache_read, cache_write=cache_write
    )


@pytest.mark.parametrize("model_id", sorted(_CURRENT_MODEL_PRICES))
def test_each_current_model_costs_more_than_zero(model_id: str) -> None:
    """Every token stream of a current model adds to the cost."""
    for usage in (
        TokenUsage(input_tokens=1, output_tokens=0),
        TokenUsage(input_tokens=0, output_tokens=1),
        TokenUsage(input_tokens=0, output_tokens=0, cache_read_tokens=1),
        TokenUsage(input_tokens=0, output_tokens=0, cache_write_tokens=1),
    ):
        assert compute_cost(usage, model_id) > 0


def test_default_model_is_sonnet_5_5() -> None:
    """The field default and the env fallback both name the current Sonnet."""
    assert Settings.model_fields["agent_default_model"].default == "anthropic:claude-sonnet-5-5"
    assert get_settings().agent_default_model == "anthropic:claude-sonnet-5-5"


def test_a_run_on_the_default_model_costs_more_than_zero() -> None:
    """The default model is priced, so its runs count against the spend limit."""
    model = get_settings().agent_default_model
    assert is_model_priced(model)
    usage = TokenUsage(input_tokens=1_200, output_tokens=500)
    assert compute_cost(usage, model) == Decimal("0.0074")


@pytest.mark.parametrize(
    ("model_id", "priced"),
    [
        ("anthropic:claude-sonnet-5-5", True),
        ("claude-sonnet-5-5", True),
        ("Anthropic:claude-sonnet-5-5", False),
        ("openai:claude-sonnet-5-5", False),
        (_UNKNOWN_MODEL, False),
        ("claude-not-a-real-model", False),
    ],
)
def test_is_model_priced_reads_a_bare_id_as_anthropic(model_id: str, priced: bool) -> None:
    """A bare id is an Anthropic id, as the runtime stores it; others must match exactly."""
    assert is_model_priced(model_id) is priced


def test_unpriced_model_gives_one_warning() -> None:
    """A model with no price row logs one warning that names it."""
    with capture_logs() as captured:
        assert warn_if_model_unpriced(_UNKNOWN_MODEL) is False
    events = [e for e in captured if e.get("event") == _UNPRICED_EVENT]
    assert len(events) == 1
    assert events[0]["model_id"] == _UNKNOWN_MODEL
    assert events[0]["log_level"] == "warning"


def test_priced_model_gives_no_warning() -> None:
    """The default model has a price, so nothing is logged."""
    with capture_logs() as captured:
        assert warn_if_model_unpriced("anthropic:claude-sonnet-5-5") is True
    assert [e for e in captured if e.get("event") == _UNPRICED_EVENT] == []


def test_start_checks_the_default_model_and_still_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The start runs the check on ``AGENT_DEFAULT_MODEL``; an unpriced model does not stop it."""
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", _UNKNOWN_MODEL)
    get_settings.cache_clear()
    spy = MagicMock(wraps=warn_if_model_unpriced)
    monkeypatch.setattr(main_module, "warn_if_model_unpriced", spy)
    with TestClient(main_module.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    spy.assert_called_once_with(_UNKNOWN_MODEL)


def test_status_says_spend_is_not_counted_for_an_unpriced_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status read names the model and says its spend is not counted."""
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", _UNKNOWN_MODEL)
    get_settings.cache_clear()
    spend = _agent_model_spend()
    assert spend.model == _UNKNOWN_MODEL
    assert spend.spend_counted is False


def test_status_says_spend_is_counted_for_the_default_model() -> None:
    """The default model is priced, so the status read says its spend is counted."""
    spend = _agent_model_spend()
    assert spend.model == "anthropic:claude-sonnet-5-5"
    assert spend.spend_counted is True
