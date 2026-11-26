# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``POST /api/v1/runner-principals/enroll`` -- a runner enrolls its public key.

The caller has no login yet. The one-time code from ``meho runner-principal
register`` is its proof. The route checks the code, puts the public key on the
runner's Keycloak client and returns what the runner needs to sign its own
login. See :mod:`meho_backplane.auth.runner_enrollment`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine, Sequence
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi import status as http_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, model_validator

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
    audit_refused_body,
    enroll_runner_key,
)
from meho_backplane.auth.runner_enrollment_limits import (
    ENROLL_LIMIT_UNAVAILABLE_DETAIL,
    ENROLL_RATE_LIMIT_DETAIL,
    EnrollRateLimitError,
    enforce_enroll_request_limit,
)

__all__ = ["router"]

#: The field names a body error may carry in its ``loc`` after the leading
#: ``body`` marker. Any other name is a field the sender added, so it is not
#: repeated.
_FIELD_NAMES: frozenset[str] = frozenset({"enrollment_code", "public_key", "runner_name"})


def _answer_loc(error: Any) -> list[int | str]:
    """The ``loc`` of *error* with each name the sender chose replaced.

    Only the first part may be the framework's ``body`` marker. An
    ``extra_forbidden`` error names the sender's field in its last part, which
    always becomes ``extra_field``.
    """
    loc = list(error.get("loc", ()))
    extra = error.get("type") == "extra_forbidden"
    answer: list[int | str] = []
    for index, part in enumerate(loc):
        marker = index == 0 and part == "body"
        field = part in _FIELD_NAMES and not (extra and index == len(loc) - 1)
        answer.append(part if isinstance(part, int) or marker or field else "extra_field")
    return answer


def _answer_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """The body-check errors of the 422 answer, without anything the sender sent.

    Each error keeps its type, its text and its place. It leaves out ``input``,
    the names of fields the sender added and every ``ctx`` value that is not a
    number (a limit such as the longest length).
    """
    answer: list[dict[str, Any]] = []
    for error in errors:
        item: dict[str, Any] = {
            "type": error["type"],
            "loc": _answer_loc(error),
            "msg": error["msg"],
        }
        ctx = {
            key: value
            for key, value in (error.get("ctx") or {}).items()
            if isinstance(value, int | float) and not isinstance(value, bool)
        }
        if ctx:
            item["ctx"] = ctx
        answer.append(item)
    return answer


async def _sent_code(request: Request) -> Any:
    """The ``enrollment_code`` of the body, or ``None`` when there is none."""
    try:
        body = json.loads(await request.body())
    except (ValueError, RecursionError):
        return None
    return body.get("enrollment_code") if isinstance(body, dict) else None


class _EnrollRoute(APIRoute):
    """Limit the requests per source address first, then check the body.

    A body that fails the check gets a 422 that repeats nothing the sender
    sent. When its code matches a runner, one audit row records the refusal.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def limited(request: Request) -> Response:
            source = request.client.host if request.client else "unknown"
            try:
                await enforce_enroll_request_limit(source)
            except EnrollRateLimitError as exc:
                raise HTTPException(
                    status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=ENROLL_RATE_LIMIT_DETAIL,
                    headers={"Retry-After": str(exc.retry_after_seconds)},
                ) from exc
            except Exception as exc:
                structlog.get_logger().warning("runner_enroll_limit_unavailable", exc_info=True)
                raise HTTPException(
                    status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=ENROLL_LIMIT_UNAVAILABLE_DETAIL,
                ) from exc
            try:
                return await handler(request)
            except RequestValidationError as exc:
                answer = JSONResponse(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"detail": _answer_errors(exc.errors())},
                )
            # The audit runs after the handler is left, so an audit error does
            # not carry the body check error (and the sent body) with it.
            await audit_refused_body(await _sent_code(request))
            return answer

        return limited


router = APIRouter(
    prefix="/api/v1/runner-principals",
    tags=["runner-principals"],
    route_class=_EnrollRoute,
)

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
    #: The published type is an object. The code is checked before the key, so
    #: a value of any type reaches :func:`enroll_runner_key`.
    public_key: Annotated[Any, WithJsonSchema({"type": "object", "additionalProperties": True})]
    runner_name: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "The name the runner was registered under (its MEHO_RUNNER_ID). "
            "A code of a runner with another name is refused before anything changes."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _missing_key_is_a_bad_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "public_key" not in data:
            return {**data, "public_key": None}
        return data


class RunnerEnrollResponse(BaseModel):
    """What the runner needs to sign its login request."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    issuer: str
    token_endpoint: str
    signing_alg: str
    key_id: str


@router.post(
    "/enroll",
    response_model=RunnerEnrollResponse,
    responses={
        429: {"description": "Too many requests from this source address."},
        502: {"description": "Keycloak cannot be reached or answers with an error."},
        503: {
            "description": (
                "The request counter cannot be reached, or the Keycloak admin "
                "client is not configured."
            )
        },
    },
)
async def enroll_runner(body: RunnerEnrollRequest) -> RunnerEnrollResponse:
    """Register a runner's public key with its one-time code.

    No login is needed: the code is the proof. A wrong, empty, used, expired or
    revoked code all get 403 ``enrollment_code_invalid``, whatever the name and
    the key are. A used code with another key gets the same 403. A good code of
    a runner with another name gets 409 ``enrollment_code_for_another_runner``,
    and the code stays usable for its own runner. A good, unused code with a
    bad key gets 422. A repeat of a used code with the key it enrolled gives
    the same answer again. A source address that sends more requests per minute
    than ``RUNNER_ENROLL_RATE_PER_MINUTE`` gets 429 ``enrollment_rate_limited``.
    When the counter cannot be reached the route answers 503
    ``enrollment_limit_unavailable``; the runner's next start tries again.
    When the Keycloak admin client is not configured it answers 503
    ``keycloak_admin_not_configured``. When Keycloak cannot be reached or
    answers with an error it answers 502 ``keycloak_admin_error``.
    A body that fails the body check gets 422 without any value the sender sent.
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
