# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projection helpers for the ``/ui/sensors`` registry console surface (#2591).

The list handler projects a
:class:`~meho_backplane.checks.schemas.SensorRead` -- the same read shape the
Bearer ``GET /api/v1/sensors`` route returns, carrying the identity
(``name`` / ``connector_id`` / ``op_id``) plus the latest-result projection
(``last_state`` / ``last_value`` / ``last_evaluated_at`` / ``state_since``) --
into the flat dict the template reads. Keeping the projection here (not in the
route module) makes the row-to-view mapping unit-testable without a FastAPI
request fixture.

The ``last_state`` badge reuses the checks surface's five-state vocabulary
(:func:`~meho_backplane.ui.routes.checks.views.state_badge_class`) so a
Sensor's latest state reads identically here and on ``/ui/checks`` -- no new
CSS state vocabulary. UTC coercion is likewise reused: the
``DateTime(timezone=True)`` columns round-trip naive on the SQLite test driver
and aware on PG, and the template does ``now_utc - ts`` arithmetic, so every
timestamp is coerced to tz-aware UTC before it reaches the template.
"""

from __future__ import annotations

from meho_backplane.checks.schemas import SensorRead
from meho_backplane.ui.routes.checks.views import coerce_utc_aware, state_badge_class

__all__ = [
    "cadence_label",
    "project_sensor_to_row",
]


def cadence_label(sensor: SensorRead) -> str:
    """Render a Sensor's cadence union into one human-readable string.

    ``interval`` -> ``"every 60s"``; ``cron`` -> ``"0 * * * * (UTC)"``. Read
    defensively: the DB-side ``ck_sensor_cadence_fields`` CHECK guarantees the
    matching column is populated, but a future cadence kind that reaches this
    projection before its display arm exists falls through to the raw
    discriminator rather than raising under ``StrictUndefined``.
    """
    kind = sensor.cadence_kind.value
    if kind == "interval" and sensor.interval_seconds is not None:
        return f"every {sensor.interval_seconds}s"
    if kind == "cron" and sensor.cron_expr:
        return f"{sensor.cron_expr} ({sensor.timezone})"
    return kind


def project_sensor_to_row(sensor: SensorRead) -> dict[str, object]:
    """Project one Sensor list row into the flat dict the template reads.

    ``last_value`` is coerced to a string (``None`` -> the em-dash placeholder
    is the template's job) so the cell never renders a Python repr; every
    timestamp is coerced tz-aware for the relative-time macro.
    """
    return {
        "id": str(sensor.id),
        "name": sensor.name,
        "connector_id": sensor.connector_id,
        "op_id": sensor.op_id,
        "status": sensor.status.value,
        "cadence_kind": sensor.cadence_kind.value,
        "cadence_label": cadence_label(sensor),
        "last_state": sensor.last_state,
        "last_state_badge": state_badge_class(sensor.last_state),
        "last_value": None if sensor.last_value is None else str(sensor.last_value),
        "last_evaluated_at": coerce_utc_aware(sensor.last_evaluated_at),
        "state_since": coerce_utc_aware(sensor.state_since),
    }
