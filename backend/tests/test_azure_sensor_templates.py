# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Azure sensor recipes: valid bodies, and the state each gives for fixture answers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.checks import evaluate_assertion
from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.dashboard_file_schemas import DashboardFile
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.connectors.azure import AZURE_CONNECTOR_ID
from meho_backplane.connectors.azure.sensor_templates import (
    TARGET_PLACEHOLDER,
    advisor_sensor_template,
    azure_dashboard_recipe,
    budget_sensor_template,
    policy_sensor_template,
)
from meho_backplane.connectors.azure.typed_ops import AZURE_TYPED_OPS

_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _state(body: dict[str, Any], payload: Any) -> tuple[str, Any]:
    outcome = evaluate_assertion(AssertionSpec.model_validate(body["assertion"]), payload, now=_NOW)
    return outcome.state, outcome.value


def _all_bodies() -> list[dict[str, Any]]:
    return [
        budget_sensor_template(target_name="sub-a"),
        policy_sensor_template(baseline=4, target_name="sub-a"),
        advisor_sensor_template(baseline=2, target_name="sub-a"),
    ]


@pytest.mark.parametrize("body", _all_bodies(), ids=lambda b: b["name"])
def test_each_recipe_is_a_valid_sensor_create_for_a_known_safe_op(body: dict[str, Any]) -> None:
    created = SensorCreate.model_validate(body)
    assert created.connector_id == AZURE_CONNECTOR_ID
    assert created.target == {"name": "sub-a"}
    op = next(o for o in AZURE_TYPED_OPS if o.op_id == created.op_id)
    assert op.safety_level == "safe"


def test_cadences() -> None:
    assert budget_sensor_template()["interval_seconds"] == 86_400
    assert policy_sensor_template(baseline=0)["interval_seconds"] == 21_600
    assert advisor_sensor_template(baseline=0)["interval_seconds"] == 86_400
    assert advisor_sensor_template(baseline=0)["params"] == {"impact": "High"}


@pytest.mark.parametrize(
    ("percents", "expected", "value"),
    [
        ([10.0, 79.99], "ok", 79.99),
        ([10.0, 80.0], "degraded", 80.0),
        ([50.0, 99.0, 12.0], "degraded", 99.0),
        ([100.0], "critical", 100.0),
        ([30.0, 140.5], "critical", 140.5),
    ],
)
def test_budget_states(percents: list[float], expected: str, value: float) -> None:
    payload = {"budgets": [{"name": f"b{i}", "percent_used": p} for i, p in enumerate(percents)]}
    assert _state(budget_sensor_template(), payload) == (expected, value)


def test_budget_with_no_budget_or_no_percent_is_unknown_not_ok() -> None:
    body = budget_sensor_template()
    assert _state(body, {"budgets": [], "count": 0})[0] == "unknown"
    # Azure gives no percent when the amount is 0.
    assert _state(body, {"budgets": [{"name": "b", "percent_used": None}]})[0] == "unknown"
    assert _state(body, {"error": "x"})[0] == "unknown"


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "ok"), (4, "ok"), (5, "degraded"), (14, "degraded"), (15, "critical"), (400, "critical")],
)
def test_policy_states_against_baseline(count: int, expected: str) -> None:
    body = policy_sensor_template(baseline=4)
    assert _state(body, {"non_compliant_resources": count, "value": []}) == (expected, count)


def test_policy_baseline_zero_and_unknown_answer() -> None:
    body = policy_sensor_template(baseline=0)
    assert _state(body, {"non_compliant_resources": 0})[0] == "ok"
    assert _state(body, {"non_compliant_resources": 1})[0] == "degraded"
    assert _state(body, {"non_compliant_resources": 11})[0] == "critical"
    # A count Azure did not give is unknown, never ok.
    assert _state(body, {"non_compliant_resources": None})[0] == "unknown"
    assert _state(body, {})[0] == "unknown"


@pytest.mark.parametrize(
    ("count", "expected"), [(2, "ok"), (3, "degraded"), (12, "degraded"), (13, "critical")]
)
def test_advisor_states_against_baseline(count: int, expected: str) -> None:
    body = advisor_sensor_template(baseline=2)
    assert _state(body, {"items": [], "count": count}) == (expected, count)


@pytest.mark.parametrize("bad", [-1, True, 2.5, "3", None])
def test_a_bad_baseline_is_refused(bad: Any) -> None:
    with pytest.raises(ValueError, match="baseline"):
        policy_sensor_template(baseline=bad)
    with pytest.raises(ValueError, match="baseline"):
        advisor_sensor_template(baseline=bad)


def test_templates_return_fresh_dicts() -> None:
    first = budget_sensor_template()
    first["assertion"]["compare"]["degraded"] = 1
    assert budget_sensor_template()["assertion"]["compare"]["degraded"] == 80


def test_dashboard_recipe_is_a_valid_dashboard_file_with_three_sensors() -> None:
    recipe = azure_dashboard_recipe(policy_baseline=3, advisor_baseline=1)
    parsed = DashboardFile.model_validate(recipe)
    assert [s.op_id for s in parsed.sensors] == [
        "azure.cost.budget.list",
        "azure.policy.compliance.summary",
        "azure.advisor.recommendation.list",
    ]
    assert parsed.placeholders() == [TARGET_PLACEHOLDER]
    assert parsed.sensors[1].assertion.compare.degraded == 3  # type: ignore[union-attr]
    for sensor in parsed.sensors:
        sensor.to_create(target_name="sub-a")


def test_the_quickstart_json_matches_the_dashboard_recipe() -> None:
    """The how-to shows the recipe with baselines 12 and 3. Keep the two equal."""
    guide = Path(__file__).resolve().parents[2] / "docs-site/guides/sensors-quickstart.md"
    text = guide.read_text(encoding="utf-8")
    start = text.index("## Watch an Azure subscription")
    block = text[start:].split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(block) == azure_dashboard_recipe(policy_baseline=12, advisor_baseline=3)
