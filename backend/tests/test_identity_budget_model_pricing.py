# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Prices for the current Claude models, and a warning for a model without one (#5771).

A model with no row in :data:`MODEL_PRICING` costs 0, so the spend limit
per login does not count runs on it. These tests check that:

* each current model has a price row, and a run on the default model
  costs more than 0;
* Haiku 5.5 charges its higher price to a request whose prompt is over
  100,000 tokens, and a run is priced one request at a time;
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
from pydantic_ai.usage import RequestUsage, RunUsage
from structlog.testing import capture_logs

from meho_backplane import main as main_module
from meho_backplane.api.v1.health import _agent_model_spend
from meho_backplane.operations.identity_budget import (
    MODEL_PRICING,
    PerMillionPricing,
    TokenUsage,
    compute_cost,
    compute_run_cost,
)
from meho_backplane.operations.unpriced_model import is_model_priced, warn_if_model_unpriced
from meho_backplane.settings import Settings, get_settings

_UNPRICED_EVENT = "agent_default_model_unpriced"
_UNKNOWN_MODEL = "anthropic:claude-not-a-real-model"
_HAIKU = "anthropic:claude-haiku-5-5"
_PER_MILLION = Decimal(1_000_000)


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
    pricing = MODEL_PRICING[model_id]
    assert (pricing.input, pricing.output, pricing.cache_read, pricing.cache_write) == tuple(
        Decimal(p) for p in prices
    )


def test_haiku_5_5_has_a_price_for_a_long_prompt() -> None:
    """Haiku 5.5 charges 5x more to a request whose prompt is over 100,000 tokens."""
    pricing = MODEL_PRICING[_HAIKU]
    assert pricing.long_prompt_over == 100_000
    assert pricing.long_prompt == PerMillionPricing(
        input=Decimal("0.50"),
        output=Decimal("2.50"),
        cache_read=Decimal("0.05"),
        cache_write=Decimal("0.625"),
    )


@pytest.mark.parametrize(
    ("prompt_tokens", "long"),
    [(99_999, False), (100_000, False), (100_001, True), (1_000_000, True)],
)
def test_haiku_5_5_request_over_100_000_prompt_tokens_costs_the_higher_price(
    prompt_tokens: int, long: bool
) -> None:
    """The threshold is per request: up to 100,000 prompt tokens is the lower price."""
    pricing = MODEL_PRICING[_HAIKU]
    rate = pricing.long_prompt if long else pricing
    assert rate is not None
    usage = TokenUsage(input_tokens=prompt_tokens, output_tokens=1_000)
    expected = (rate.input * prompt_tokens + rate.output * 1_000) / _PER_MILLION
    assert compute_cost(usage, _HAIKU) == expected


def test_cached_prompt_tokens_count_towards_the_long_prompt_threshold() -> None:
    """The prompt length is every input stream: uncached, cache read and cache write."""
    usage = TokenUsage(
        input_tokens=40_000, output_tokens=0, cache_read_tokens=50_000, cache_write_tokens=10_001
    )
    assert usage.prompt_tokens == 100_001
    rate = MODEL_PRICING[_HAIKU].long_prompt
    assert rate is not None
    expected = (
        rate.input * 40_000 + rate.cache_read * 50_000 + rate.cache_write * 10_001
    ) / _PER_MILLION
    assert compute_cost(usage, _HAIKU) == expected


def test_framework_usage_counts_a_cached_token_once() -> None:
    """The framework's input total includes the cached tokens; the lift takes them out.

    pydantic_ai 2.x reports ``input_tokens`` as the whole prompt, cached
    tokens included. So a request of 80,000 prompt tokens with 60,000
    read from the cache has 20,000 uncached input tokens, not 80,000.
    """
    usage = TokenUsage.from_request_usage(
        RequestUsage(input_tokens=80_000, output_tokens=100, cache_read_tokens=60_000)
    )
    assert usage == TokenUsage(input_tokens=20_000, output_tokens=100, cache_read_tokens=60_000)
    assert usage.prompt_tokens == 80_000
    assert usage.total_tokens == 80_100
    pricing = MODEL_PRICING[_HAIKU]
    assert compute_cost(usage, _HAIKU) == Decimal("0.00265")
    assert (
        compute_cost(usage, _HAIKU)
        == (pricing.input * 20_000 + pricing.cache_read * 60_000 + pricing.output * 100)
        / _PER_MILLION
    )


def test_run_usage_is_split_the_same_way() -> None:
    """A whole run's totals get the same split as one request."""
    usage = TokenUsage.from_run_usage(
        RunUsage(
            requests=2,
            input_tokens=80_000,
            output_tokens=100,
            cache_read_tokens=50_000,
            cache_write_tokens=10_000,
        )
    )
    assert usage == TokenUsage(
        input_tokens=20_000, output_tokens=100, cache_read_tokens=50_000, cache_write_tokens=10_000
    )


def test_more_cached_than_input_tokens_gives_no_negative_uncached_count() -> None:
    """A usage that does not follow the framework's convention stays at zero, not below."""
    usage = TokenUsage.from_request_usage(RequestUsage(input_tokens=10, cache_read_tokens=20))
    assert usage.input_tokens == 0
    assert usage.cache_read_tokens == 20


@pytest.mark.parametrize(
    ("prompt_tokens", "long"), [(99_999, False), (100_000, False), (100_001, True)]
)
def test_framework_usage_with_cached_tokens_around_the_long_prompt_threshold(
    prompt_tokens: int, long: bool
) -> None:
    """The threshold compares the whole prompt, with the cached tokens counted once."""
    cached = 60_000
    usage = TokenUsage.from_request_usage(
        RequestUsage(input_tokens=prompt_tokens, output_tokens=100, cache_read_tokens=cached)
    )
    assert usage.prompt_tokens == prompt_tokens
    pricing = MODEL_PRICING[_HAIKU]
    rate = pricing.long_prompt if long else pricing
    assert rate is not None
    expected = (
        rate.input * (prompt_tokens - cached) + rate.cache_read * cached + rate.output * 100
    ) / _PER_MILLION
    assert compute_cost(usage, _HAIKU) == expected


def test_run_cost_prices_each_request_on_its_own_rate() -> None:
    """A run adds up its requests, each at the rate its own prompt length picks."""
    short = TokenUsage(input_tokens=60_000, output_tokens=500)
    long = TokenUsage(input_tokens=100_001, output_tokens=500)
    assert compute_run_cost([short, long], _HAIKU) == compute_cost(short, _HAIKU) + compute_cost(
        long, _HAIKU
    )
    # Two short prompts add up to more than the threshold, but each
    # request stays at the lower price.
    assert compute_run_cost([short, short], _HAIKU) == 2 * compute_cost(short, _HAIKU)
    as_one_request = TokenUsage(input_tokens=120_000, output_tokens=1_000)
    assert compute_run_cost([short, short], _HAIKU) < compute_cost(as_one_request, _HAIKU)


def test_run_without_requests_costs_nothing() -> None:
    assert compute_run_cost([], _HAIKU) == Decimal(0)


def test_a_model_with_one_price_charges_it_at_any_prompt_length() -> None:
    """Sonnet 5.5 has no long-prompt price, so a long request costs the same rate."""
    model = "anthropic:claude-sonnet-5-5"
    pricing = MODEL_PRICING[model]
    assert pricing.long_prompt is None
    usage = TokenUsage(input_tokens=200_000, output_tokens=1)
    expected = (pricing.input * 200_000 + pricing.output * 1) / _PER_MILLION
    assert compute_cost(usage, model) == expected


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
