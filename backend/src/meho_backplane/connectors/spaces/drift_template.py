# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Default health-check template for the nightly drift check.

A check (Sensor) pins an operation, parameters, an assertion, a cadence and a
severity. This module holds the recommended tuple for ``spaces.drift`` as a
ready-to-send ``POST /api/v1/sensors`` body:

* once a night, at 02:00 UTC;
* ``degraded`` when ``total_count`` is above the ``degraded_above`` bound
  (default 0, so any drift);
* ``critical`` only when the operator passes ``critical_above``.

The check only reports. It never changes an assignment. The bounds are the
arguments of :func:`drift_sensor_template`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.spaces.common import SPACES_CONNECTOR_ID

__all__ = ["DRIFT_SENSOR_NAME", "drift_sensor_template"]

DRIFT_SENSOR_NAME: Final[str] = "spaces-drift"
_NIGHTLY_CRON: Final[str] = "0 2 * * *"


def drift_sensor_template(
    *,
    name: str = DRIFT_SENSOR_NAME,
    degraded_above: float = 0,
    critical_above: float | None = None,
) -> dict[str, Any]:
    """Return a new check body. Change the name and the bounds before sending it."""
    compare: dict[str, Any] = {"type": "threshold", "op": "gt", "degraded": degraded_above}
    if critical_above is not None:
        compare["critical"] = critical_above
    return {
        "name": name,
        "connector_id": SPACES_CONNECTOR_ID,
        "op_id": "spaces.drift",
        "target": None,
        "params": {},
        "assertion": {"select": {"path": "$.total_count"}, "compare": compare},
        "cadence_kind": "cron",
        "cron_expr": _NIGHTLY_CRON,
        "timezone": "UTC",
        "severity": "degraded" if critical_above is None else "critical",
    }
