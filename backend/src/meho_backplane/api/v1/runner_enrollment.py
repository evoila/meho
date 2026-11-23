# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/runner-principals/enroll`` -- a runner enrolls its public key.

The caller has no login yet. The one-time code from ``meho runner-principal
register`` is its proof. The route checks the code, puts the public key on the
runner's Keycloak client and returns what the runner needs to sign its own
login. See :mod:`meho_backplane.auth.runner_enrollment`.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.keycloak_admin import (
    KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.runner_enrollment import (
    EnrollmentCodeInvalidError,
    EnrollmentKeyInvalidError,
    enroll_runner_key,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/runner-principals", tags=["runner-principals"])

#: Bounds the body: a code is about 90 characters; a P-256 key is small.
_CODE_MAX_LENGTH = 256


class RunnerEnrollRequest(BaseModel):
    """A runner's one-time code and its public key (a JWK, EC P-256)."""

    model_config = ConfigDict(extra="forbid")

    enrollment_code: str = Field(min_length=1, max_length=_CODE_MAX_LENGTH)
    public_key: dict[str, Any]


class RunnerEnrollResponse(BaseModel):
    """What the runner needs to sign its login request."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    issuer: str
    token_endpoint: str
    signing_alg: str
    key_id: str


@router.post("/enroll", response_model=RunnerEnrollResponse)
async def enroll_runner(body: RunnerEnrollRequest) -> RunnerEnrollResponse:
    """Register a runner's public key with its one-time code.

    No login is needed: the code is the proof. A wrong, used, expired or
    revoked code all get 403 ``enrollment_code_invalid``. A bad key gets 422.
    """
    try:
        result = await enroll_runner_key(body.enrollment_code, body.public_key)
    except EnrollmentCodeInvalidError as exc:
        structlog.get_logger().info("runner_enrollment_refused")
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="enrollment_code_invalid"
        ) from exc
    except EnrollmentKeyInvalidError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except KeycloakAdminNotConfiguredError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
        ) from exc
    except KeycloakAdminError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY, detail="keycloak_admin_error"
        ) from exc
    return RunnerEnrollResponse(
        client_id=result.client_id,
        issuer=result.issuer,
        token_endpoint=result.token_endpoint,
        signing_alg=result.signing_alg,
        key_id=result.key_id,
    )
