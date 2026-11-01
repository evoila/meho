# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``fleet.product.certificate.replace``: a vendor job with a pre-validation.

The steps:

1. ``GET /lcm/request/api/v2/requests`` -- is a certificate request for the
   same environment and product still running? Then return its id and
   start nothing. This is a help, not a lock.
2. ``POST .../certificates/{certificateId}/pre-validate`` -- starts the
   pre-validation, a lifecycle request of its own.
3. ``GET /lcm/request/api/v2/requests/{requestId}/pre-validation-report``
   until the report has ended (at most :data:`PRE_VALIDATION_WAIT_SECONDS`).
4. ``PUT .../certificates/{certificateId}`` -- starts the replace. The
   answer says ``accepted`` with the vendor's request id; the caller reads
   it with ``fleet.request.get`` until it ends.

A failed step stops the op. The answer lists every step as ``done``,
``failed``, ``unknown`` (sent, but the reply was lost) or ``not_run``.
Nothing is undone. The connector sends HTTP Basic on every request, so no
session can expire between the start and a status read.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors.vcf_fleet.locker import path_id, vendor_error_text

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_fleet.connector import VcfFleetConnector
    from meho_backplane.connectors.vcf_fleet.session import VcfFleetTargetLike

__all__ = [
    "PRE_VALIDATION_WAIT_SECONDS",
    "STATUS_READ_OP_ID",
    "VENDOR_CALLS",
    "product_certificate_replace",
]

PRODUCT_CERTIFICATE_PATH: Final = (
    "/lcm/lcops/api/v2/environments/{environmentId}/products/{productId}"
    "/certificates/{certificateId}"
)
PRODUCT_CERTIFICATE_PRE_VALIDATE_PATH: Final = PRODUCT_CERTIFICATE_PATH + "/pre-validate"
REQUESTS_PATH: Final = "/lcm/request/api/v2/requests"
PRE_VALIDATION_REPORT_PATH: Final = "/lcm/request/api/v2/requests/{requestId}/pre-validation-report"

#: Every vendor call of this module, as ``(method, path)``. The reconcile
#: lane checks them against the pinned API file.
VENDOR_CALLS: Final[tuple[tuple[str, str], ...]] = (
    ("GET", REQUESTS_PATH),
    ("POST", PRODUCT_CERTIFICATE_PRE_VALIDATE_PATH),
    ("GET", PRE_VALIDATION_REPORT_PATH),
    ("PUT", PRODUCT_CERTIFICATE_PATH),
)

#: The op that reads a lifecycle request until it ends.
STATUS_READ_OP_ID: Final = "fleet.request.get"

#: How long the replace waits for the pre-validation report, and how often it reads it.
PRE_VALIDATION_WAIT_SECONDS: Final = 180.0
PRE_VALIDATION_POLL_SECONDS: Final = 5.0

#: Request states of a lifecycle request that has not ended yet.
_RUNNING_STATES: Final = frozenset({"CREATED", "SUBMITTED", "QUEUED", "INPROGRESS", "IN_PROGRESS"})
_ENDED_REPORT_STATES: Final = frozenset({"COMPLETED", "FAILED", "CANCELLED", "ABORTED"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _Steps:
    """The steps of the replace: ``done``, ``failed``, ``unknown`` or ``not_run``."""

    NAMES: Final = ("check_running_requests", "pre_validate", "wait_for_pre_validation", "replace")

    def __init__(self) -> None:
        self._rows = [{"step": name, "result": "not_run", "error": None} for name in self.NAMES]

    def set(self, name: str, result: str, error: str | None = None) -> None:
        for row in self._rows:
            if row["step"] == name:
                row["result"], row["error"] = result, error

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


def _error_text(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}: {vendor_error_text(exc.response)}"
    if isinstance(exc, httpx.TransportError):
        return f"not reachable at {_now()}: {type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _failed(steps: _Steps, step: str, exc: Exception, **extra: Any) -> dict[str, Any]:
    error = _error_text(exc)
    steps.set(step, "failed", error)
    return {"status": "failed", "failed_step": step, "error": error, **extra, "steps": steps.rows()}


def _is_same_job(request: Any, environment_id: str, product_id: str) -> bool:
    """A running certificate request on the same product (a help, not a lock)."""
    if not isinstance(request, dict):
        return False
    if str(request.get("state") or "").upper() not in _RUNNING_STATES:
        return False
    kind = " ".join(
        str(request.get(key) or "") for key in ("requestType", "requestName", "requestReason")
    )
    if "certificate" not in kind.lower():
        return False
    inputs = request.get("inputMap")
    text = inputs if isinstance(inputs, str) else json.dumps(inputs, default=str)
    return environment_id in text and product_id in text


async def _running_request(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    environment_id: str,
    product_id: str,
) -> dict[str, Any] | None:
    requests: Any = await connector._get_json(target, REQUESTS_PATH, operator=operator)
    for request in requests if isinstance(requests, list) else []:
        if _is_same_job(request, environment_id, product_id):
            return dict(request)
    return None


def _failing_checks(elements: Any) -> list[dict[str, Any]]:
    """Every check of a pre-validation report whose status says error or failure."""
    found: list[dict[str, Any]] = []
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict):
            continue
        status = str(element.get("status") or "").upper()
        if "FAIL" in status or "ERROR" in status:
            found.append(
                {
                    "check": element.get("checkName"),
                    "status": element.get("status"),
                    "result": element.get("resultDescription"),
                    "recommendations": element.get("recommendations") or [],
                }
            )
        found.extend(_failing_checks(element.get("childElements")))
    return found


async def _wait_for_report(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    request_id: str,
) -> dict[str, Any] | None:
    """The pre-validation report once it has ended, or ``None`` after the wait."""
    path = PRE_VALIDATION_REPORT_PATH.format(requestId=quote(request_id, safe=""))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PRE_VALIDATION_WAIT_SECONDS
    while True:
        try:
            report: Any = await connector._get_json(target, path, operator=operator)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            report = None
        state = str(report.get("requestState") or "").upper() if isinstance(report, dict) else ""
        if isinstance(report, dict) and state in _ENDED_REPORT_STATES:
            return report
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(PRE_VALIDATION_POLL_SECONDS)


def _report_problem(report: dict[str, Any]) -> dict[str, Any] | None:
    """Why a finished pre-validation does not allow the replace, or ``None``."""
    state = str(report.get("requestState") or "").upper()
    if state == "COMPLETED" and report.get("overallStatus") is True:
        return None
    return {
        "request_state": report.get("requestState"),
        "overall_status": report.get("overallStatus"),
        "failing_checks": _failing_checks(report.get("rootValidations")),
    }


async def _pre_validate(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    pre_path: str,
    steps: _Steps,
) -> tuple[str | None, dict[str, Any] | None]:
    """Run the pre-validation. Returns its request id and, on failure, the answer."""
    try:
        started = await connector._post_json(target, pre_path, operator=operator)
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        return None, _failed(steps, "pre_validate", exc)
    pre_id = str(started.get("requestId") or "") if isinstance(started, dict) else ""
    if not pre_id:
        return None, _failed(steps, "pre_validate", ValueError("the appliance gave no requestId"))
    steps.set("pre_validate", "done")
    try:
        report = await _wait_for_report(connector, operator, target, pre_id)
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        step = "wait_for_pre_validation"
        return pre_id, _failed(steps, step, exc, pre_validation_request_id=pre_id)
    if report is None:
        error = f"the pre-validation did not end within {int(PRE_VALIDATION_WAIT_SECONDS)} seconds"
        steps.set("wait_for_pre_validation", "failed", error)
        answer = {"status": "failed", "failed_step": "wait_for_pre_validation", "error": error}
        return pre_id, {**answer, "pre_validation_request_id": pre_id, "steps": steps.rows()}
    problem = _report_problem(report)
    if problem is not None:
        steps.set("wait_for_pre_validation", "failed", "the pre-validation found problems")
        answer = {"status": "failed", "failed_step": "wait_for_pre_validation"}
        answer["error"] = "the pre-validation found problems; nothing was replaced"
        return pre_id, {
            **answer,
            "pre_validation_request_id": pre_id,
            "pre_validation": problem,
            "steps": steps.rows(),
        }
    steps.set("wait_for_pre_validation", "done")
    return pre_id, None


async def product_certificate_replace(
    connector: VcfFleetConnector,
    operator: Operator,
    target: VcfFleetTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``fleet.product.certificate.replace``: check, pre-validate, then start the replace."""
    environment_id = path_id(params, "environment_id")
    product_id = path_id(params, "product_id")
    certificate_id = path_id(params, "certificate_id")
    ids = {
        "environmentId": environment_id,
        "productId": product_id,
        "certificateId": certificate_id,
    }
    put_path = PRODUCT_CERTIFICATE_PATH.format(**ids)
    pre_path = PRODUCT_CERTIFICATE_PRE_VALIDATE_PATH.format(**ids)
    steps = _Steps()
    try:
        running = await _running_request(connector, operator, target, environment_id, product_id)
    except (httpx.HTTPStatusError, httpx.TransportError) as exc:
        return _failed(steps, "check_running_requests", exc)
    steps.set("check_running_requests", "done")
    if running is not None:
        return {
            "status": "already_running",
            "request_id": running.get("vmid"),
            "request_state": running.get("state"),
            "status_read": STATUS_READ_OP_ID,
            "steps": steps.rows(),
        }
    pre_id, failure = await _pre_validate(connector, operator, target, pre_path, steps)
    if failure is not None:
        return failure
    try:
        started = await connector._post_json(target, put_path, operator=operator, verb="PUT")
    except httpx.TransportError as exc:
        steps.set("replace", "unknown", f"the reply was lost: {_error_text(exc)}")
        return {
            "status": "unknown",
            "error": (
                "the replace was sent but its reply was lost; read the requests before a retry"
            ),
            "pre_validation_request_id": pre_id,
            "status_read": STATUS_READ_OP_ID,
            "steps": steps.rows(),
        }
    except httpx.HTTPStatusError as exc:
        return _failed(steps, "replace", exc, pre_validation_request_id=pre_id)
    steps.set("replace", "done")
    return {
        "status": "accepted",
        "request_id": started.get("requestId") if isinstance(started, dict) else None,
        "pre_validation_request_id": pre_id,
        "status_read": STATUS_READ_OP_ID,
        "steps": steps.rows(),
    }
