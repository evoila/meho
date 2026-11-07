# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/sensors/{sensor_id}/acknowledgement`` -- acknowledge a finding (#4128).

A person acknowledges a Sensor that is not green: until when, why (one of
``accepted_risk`` / ``fix_planned`` / ``false_alarm``) and a short text.
MEHO records who. The Sensor keeps running; while the acknowledgement is
active the advisory says "acknowledged" and the warning mails are not sent
again. See :mod:`meho_backplane.checks.acknowledgement`.

* Role ``operator``, and only a person: an agent, service or runner token
  gets 403 (``require_human_principal``). There is no MCP tool for this.
* No end time (or one without a time zone) is 422 at the body. An end time
  in the past or more than 90 days away is 422
  ``sensor_ack_invalid_end_time``.
* A green or never-checked Sensor is 422 ``sensor_ack_not_failing``.
* A Sensor that already has an active acknowledgement is 409
  ``sensor_ack_already_open``.
* A Sensor in another tenant, or no Sensor, is 404 ``sensor_not_found``.

The request's audit row (``op_id=sensor.acknowledge``) carries the new
acknowledgement under ``after``. MEHO writes its own audit rows when it
ends (``sensor.acknowledgement.expire`` / ``sensor.acknowledgement.end``).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi import status as http_status

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_human_principal, require_role
from meho_backplane.checks.acknowledgement import (
    SensorAckAlreadyOpenError,
    SensorAckEndTimeError,
    SensorAckNotFailingError,
    SensorAcknowledgementCreate,
    SensorAcknowledgementRead,
    acknowledge_sensor,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/sensors", tags=["sensors"])

_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_human = Depends(require_human_principal)


@router.post(
    "/{sensor_id}/acknowledgement",
    response_model=SensorAcknowledgementRead,
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[_require_human],
)
async def acknowledge_sensor_finding(
    sensor_id: Annotated[uuid.UUID, Path()],
    body: SensorAcknowledgementCreate,
    operator: Annotated[Operator, _require_operator],
) -> SensorAcknowledgementRead:
    """Acknowledge one Sensor finding of the caller's tenant: until when and why."""
    structlog.contextvars.bind_contextvars(
        audit_op_id="sensor.acknowledge",
        audit_op_class="write",
        audit_sensor_id=str(sensor_id),
    )
    try:
        ack = await acknowledge_sensor(
            tenant_id=operator.tenant_id,
            sensor_id=sensor_id,
            created_by_sub=operator.sub,
            payload=body,
        )
    except (SensorAckEndTimeError, SensorAckNotFailingError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.error_code,
        ) from exc
    except SensorAckAlreadyOpenError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=exc.error_code,
        ) from exc
    if ack is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sensor_not_found",
        )
    structlog.contextvars.bind_contextvars(audit_after=ack.model_dump(mode="json"))
    return ack
