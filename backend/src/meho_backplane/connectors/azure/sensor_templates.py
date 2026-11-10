# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Sensor recipes for an Azure subscription: budget, policy and Advisor.

Each function returns a ready ``POST /api/v1/sensors`` body (a fresh dict).
:func:`azure_dashboard_recipe` returns the three sensors as one dashboard file
(``meho dashboard import``).

The three recipes and what each one measures:

* Budget: the highest ``percent_used`` over the budgets, in the current
  budget period (Azure resets the spend at the start of each period).
  ``degraded`` at 80 and ``critical`` at 100. Checked once a day.
* Policy: the number of non-compliant resources right now. The baseline is
  that count on the day the sensor is made. ``degraded`` above the baseline,
  ``critical`` more than 10 above it. Checked every 6 hours.
* Advisor: the number of high-impact recommendations that are open right now.
  The same baseline rule. Checked once a day.

The baseline is a parameter: read the count once, then pass it in. A baseline
of 0 is allowed.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure import AZURE_CONNECTOR_ID

__all__ = [
    "ADVISOR_SENSOR_NAME",
    "BUDGET_SENSOR_NAME",
    "COUNT_CRITICAL_MARGIN",
    "DASHBOARD_FILE_FORMAT",
    "POLICY_SENSOR_NAME",
    "TARGET_PLACEHOLDER",
    "advisor_sensor_template",
    "azure_dashboard_recipe",
    "budget_sensor_template",
    "policy_sensor_template",
]

BUDGET_SENSOR_NAME: Final[str] = "azure-budget-used"
POLICY_SENSOR_NAME: Final[str] = "azure-policy-non-compliant"
ADVISOR_SENSOR_NAME: Final[str] = "azure-advisor-high-impact"

#: How far above the baseline a count must be to turn ``critical``.
COUNT_CRITICAL_MARGIN: Final[int] = 10

#: The format tag of a dashboard file (the same as ``checks.dashboard_file_schemas``).
DASHBOARD_FILE_FORMAT: Final[str] = "meho-dashboard/1"

#: The target name in the dashboard file. ``meho dashboard import`` maps it
#: to the name of an Azure target with ``--map-target``.
TARGET_PLACEHOLDER: Final[str] = "azure-subscription"

_ONE_DAY_SECONDS: Final[int] = 86_400
_SIX_HOURS_SECONDS: Final[int] = 21_600


def _body(
    *,
    name: str,
    op_id: str,
    target_name: str | None,
    params: dict[str, Any],
    assertion: dict[str, Any],
    interval_seconds: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "connector_id": AZURE_CONNECTOR_ID,
        "op_id": op_id,
        "target": None if target_name is None else {"name": target_name},
        "params": params,
        "assertion": assertion,
        "cadence_kind": "interval",
        "interval_seconds": interval_seconds,
        "severity": "critical",
    }


def _count_compare(baseline: int) -> dict[str, Any]:
    if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
        raise ValueError(f"baseline must be a whole number, 0 or more, got {baseline!r}")
    return {
        "type": "threshold",
        "op": "gt",
        "degraded": baseline,
        "critical": baseline + COUNT_CRITICAL_MARGIN,
    }


def budget_sensor_template(
    *, target_name: str | None = None, name: str = BUDGET_SENSOR_NAME
) -> dict[str, Any]:
    """Percent of the budget used in the current budget period (the highest budget)."""
    return _body(
        name=name,
        op_id="azure.cost.budget.list",
        target_name=target_name,
        params={},
        assertion={
            "select": {"path": "$.budgets[*].percent_used", "aggregate": "max"},
            "compare": {"type": "threshold", "op": "gte", "degraded": 80, "critical": 100},
        },
        interval_seconds=_ONE_DAY_SECONDS,
    )


def policy_sensor_template(
    *, baseline: int, target_name: str | None = None, name: str = POLICY_SENSOR_NAME
) -> dict[str, Any]:
    """Non-compliant resources now, against the count on the day the sensor is made."""
    return _body(
        name=name,
        op_id="azure.policy.compliance.summary",
        target_name=target_name,
        params={},
        assertion={
            "select": {"path": "$.non_compliant_resources"},
            "compare": _count_compare(baseline),
        },
        interval_seconds=_SIX_HOURS_SECONDS,
    )


def advisor_sensor_template(
    *, baseline: int, target_name: str | None = None, name: str = ADVISOR_SENSOR_NAME
) -> dict[str, Any]:
    """Open high-impact Advisor recommendations now, against the count today."""
    return _body(
        name=name,
        op_id="azure.advisor.recommendation.list",
        target_name=target_name,
        params={"impact": "High"},
        assertion={
            "select": {"path": "$.count"},
            "compare": _count_compare(baseline),
        },
        interval_seconds=_ONE_DAY_SECONDS,
    )


def azure_dashboard_recipe(
    *,
    policy_baseline: int,
    advisor_baseline: int,
    name: str = "azure-subscription-health",
) -> dict[str, Any]:
    """The three sensors as one dashboard file, with a target placeholder."""
    sensors = []
    for body in (
        budget_sensor_template(target_name=TARGET_PLACEHOLDER),
        policy_sensor_template(baseline=policy_baseline, target_name=TARGET_PLACEHOLDER),
        advisor_sensor_template(baseline=advisor_baseline, target_name=TARGET_PLACEHOLDER),
    ):
        body["target"] = TARGET_PLACEHOLDER
        sensors.append(body)
    return {
        "format": DASHBOARD_FILE_FORMAT,
        "dashboard": {
            "name": name,
            "description": (
                "One Azure subscription: budget used in the current period, "
                "non-compliant resources and high-impact Advisor recommendations "
                "against their baseline."
            ),
            "notify_min_state": "critical",
        },
        "sensors": sensors,
    }
