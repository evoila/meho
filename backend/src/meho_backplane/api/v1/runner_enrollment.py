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

from meho_backplane.api.v1._errors import http_for, register_error
from meho_backplane.auth.keycloak_admin import (
    KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL,
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.runner_enrollment import (
    EnrollmentCodeInvalidError,
    EnrollmentKeyInvalidError,
    EnrollmentRunnerMismatchError,
    enroll_runner_key,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/runner-principals", tags=["runner-principals"])

register_error(
    EnrollmentKeyInvalidError,
    status=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
    type_tag="runner_public_key_invalid",
    loc=("body", "public_key"),
)


class RunnerEnrollRequest(BaseModel):
    """A runner's one-time code, its public key (a JWK, EC P-256) and its name."""

    model_config = ConfigDict(extra="forbid")

    enrollment_code: str
    public_key: dict[str, Any]
    runner_name: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "The name the runner was registered under (its MEHO_RUNNER_ID). "
            "A code of a runner with another name is refused before anything changes."
        ),
    )


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

    No login is needed: the code is the proof. A wrong, empty, used, expired or
    revoked code all get 403 ``enrollment_code_invalid``. A good code of a
    runner with another name gets 409 ``enrollment_code_for_another_runner``,
    and the code stays usable for its own runner. A good code with a bad
    key gets 422. A repeat of a used code with the key it enrolled gives the
    same answer again.
    """
    try:
        result = await enroll_runner_key(
            body.enrollment_code, body.public_key, runner_name=body.runner_name
        )
    except EnrollmentCodeInvalidError as exc:
        structlog.get_logger().info("runner_enrollment_refused")
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="enrollment_code_invalid"
        ) from exc
    except EnrollmentRunnerMismatchError as exc:
        structlog.get_logger().info("runner_enrollment_refused_other_runner")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="enrollment_code_for_another_runner",
        ) from exc
    except EnrollmentKeyInvalidError as exc:
        raise http_for(exc) from exc
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
