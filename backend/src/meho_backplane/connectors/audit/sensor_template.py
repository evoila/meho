# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Default Sensor template for the loose-rows check (#4670).

A Sensor pins an operation, parameters, an assertion, a cadence and a
severity. This module holds the recommended tuple for ``audit.loose_rows``
as a ready-to-send ``POST /api/v1/sensors`` body:

* cadence: once an hour;
* ``degraded`` when at least one loose row is younger than a day;
* ``critical`` when more than ten are.

The assertion selects ``$.recent_count`` (loose rows inside the default
one-day window) and compares it with ``gt``: ``> 0`` is ``degraded``,
``> 10`` is ``critical``. ``severity`` is ``critical`` so the dashboard
cap never hides the critical band (``checks/rollup.py``). A multi-step
operation that is still running also has loose rows until it saves its
own row, so a ``degraded`` sample can clear by itself on the next run.

:func:`loose_rows_sensor_template` returns a fresh dict; the operator may
change ``name`` and ``interval_seconds`` before sending it.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final

from meho_backplane.connectors.audit.ops import (
    AUDIT_INTEGRITY_CONNECTOR_ID,
    AUDIT_LOOSE_ROWS_OP_ID,
)

__all__ = [
    "LOOSE_ROWS_SENSOR_NAME",
    "LOOSE_ROWS_SENSOR_TEMPLATE",
    "loose_rows_sensor_template",
]

LOOSE_ROWS_SENSOR_NAME: Final[str] = "audit-loose-rows"

LOOSE_ROWS_SENSOR_TEMPLATE: Final[dict[str, Any]] = {
    "name": LOOSE_ROWS_SENSOR_NAME,
    "connector_id": AUDIT_INTEGRITY_CONNECTOR_ID,
    "op_id": AUDIT_LOOSE_ROWS_OP_ID,
    "target": None,
    "params": {},
    "assertion": {
        "select": {"path": "$.recent_count"},
        "compare": {"type": "threshold", "op": "gt", "degraded": 0, "critical": 10},
    },
    "cadence_kind": "interval",
    "interval_seconds": 3600,
    "severity": "critical",
}


def loose_rows_sensor_template(*, name: str = LOOSE_ROWS_SENSOR_NAME) -> dict[str, Any]:
    """Return a copy of the default template, optionally under another name."""
    body = deepcopy(LOOSE_ROWS_SENSOR_TEMPLATE)
    body["name"] = name
    return body
