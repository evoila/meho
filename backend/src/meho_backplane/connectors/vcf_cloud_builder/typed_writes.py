# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The changes of the Cloud Builder operations.

* ``installer.cb.validation.start`` — ``POST /v1/sddcs/validations``: check a
  bring-up spec.
* ``installer.cb.sddc.start`` — ``POST /v1/sddcs``: start the bring-up.
* ``installer.cb.sddc.retry`` — ``PATCH /v1/sddcs/{id}``: retry a failed
  bring-up.

Each change runs three steps and returns all of them:

1. ``read_secrets`` — read the passwords the call names by Vault reference
   and put them into a copy of the spec (see
   :func:`~meho_backplane.connectors.vcf_cloud_builder._jobs.fill_secrets`).
2. ``check_running`` — look for a job of the same kind that runs now
   (``GET /v1/sddcs/validations``, ``GET /v1/sddcs``, or the bring-up itself
   for a retry). When one runs, return its id and start nothing. A validation
   whose cancel is still in progress counts as running. This check is a help,
   not a lock: two callers can both check before either starts.
3. ``start`` — send the start. Cloud Builder accepts the job and runs it by
   itself, so the answer says ``accepted`` with the vendor's id and the
   status read that follows it. It never says the job is done.

A vendor error comes back in the answer (``start_failed`` with the vendor's
text); a lost reply comes back as ``start_unknown``: the caller must read the
status before it tries again. Steps that ran are not undone.

An accepted start that sent a spec is remembered with the secrets of that
spec, so later status reads can hide them. A retry without a spec is not: the
job keeps the spec of its first start, and when this MEHO did not send it
(for example before a restart), the status reads keep hiding every vendor
error text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_cloud_builder._jobs import (
    ACTIVE_VALIDATION_VENDOR_STATES,
    SecretInputError,
    StepLog,
    fill_secrets,
    unreachable_reason,
    vendor_error_text,
)
from meho_backplane.connectors.vcf_cloud_builder.typed_reads import (
    SDDC_LIST_PATH,
    SDDC_PATH,
    VALIDATIONS_PATH,
    page_elements,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_cloud_builder.connector import CloudBuilderConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "SDDC_RETRY_OP_ID",
    "SDDC_START_OP_ID",
    "SDDC_STATUS_OP_ID",
    "STEPS",
    "VALIDATION_START_OP_ID",
    "VALIDATION_STATUS_OP_ID",
    "WRITE_CALLS",
    "sddc_retry_impl",
    "sddc_start_impl",
    "validation_start_impl",
]

VALIDATION_START_OP_ID: Final = "installer.cb.validation.start"
VALIDATION_STATUS_OP_ID: Final = "installer.cb.validation.status"
SDDC_START_OP_ID: Final = "installer.cb.sddc.start"
SDDC_STATUS_OP_ID: Final = "installer.cb.sddc.status"
SDDC_RETRY_OP_ID: Final = "installer.cb.sddc.retry"

#: Every ``METHOD:/path`` the changes send. The reconcile lane checks each one.
WRITE_CALLS: Final[frozenset[str]] = frozenset(
    {f"POST:{VALIDATIONS_PATH}", f"POST:{SDDC_LIST_PATH}", f"PATCH:{SDDC_PATH}"}
)

#: The steps of every change, in order.
STEPS: Final[tuple[str, ...]] = ("read_secrets", "check_running", "start")


def _running_validation(payload: Any) -> dict[str, Any] | None:
    for item in page_elements(payload):
        if item.get("executionStatus") in ACTIVE_VALIDATION_VENDOR_STATES:
            return {"id": item.get("id"), "vendor_state": item.get("executionStatus")}
    return None


def _running_bringup(payload: Any) -> dict[str, Any] | None:
    for item in page_elements(payload):
        if item.get("status") == "IN_PROGRESS":
            return {"id": item.get("id"), "vendor_state": item.get("status")}
    return None


def _running_self(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict) and payload.get("status") == "IN_PROGRESS":
        return {"id": payload.get("id"), "vendor_state": payload.get("status")}
    return None


@dataclass(frozen=True)
class _Plan:
    """What one change checks and sends."""

    kind: str
    status_op: str
    check_path: str
    find_running: Callable[[Any], dict[str, Any] | None]
    method: str
    path: str
    state_key: str


@dataclass
class _Run:
    plan: _Plan
    target: Any
    steps: StepLog = field(default_factory=lambda: StepLog(STEPS))
    secrets: tuple[str, ...] = ()

    def answer(
        self,
        status: str,
        guidance: str,
        *,
        job: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        job_id = (job or {}).get("id")
        return {
            "status": status,
            "job": {
                "kind": self.plan.kind,
                "id": job_id,
                "vendor_state": (job or {}).get("vendor_state"),
                "target": str(getattr(self.target, "name", "")),
            },
            "status_read": {
                "op_id": self.plan.status_op,
                "params": {"id": job_id} if job_id else {},
            },
            "steps": self.steps.rows(),
            "error": error,
            "guidance": guidance,
        }


async def _read_secrets(
    operator: Operator, run: _Run, params: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Step 1. Returns ``(spec, None)``, or ``(None, answer)`` when the call stops."""
    spec = params.get("spec")
    refs = params.get("secrets") or []
    if spec is None:
        run.steps.done("read_secrets")
        return None, None
    try:
        filled, run.secrets = await fill_secrets(operator, run.target, spec, refs)
    except SecretInputError as exc:
        run.steps.failed("read_secrets", str(exc))
        return None, run.answer(
            "invalid_input",
            "Fix the secret reference, then call this again. Nothing was sent.",
            error=str(exc),
        )
    run.steps.done("read_secrets")
    return filled, None


async def _check_running(
    connector: CloudBuilderConnector, operator: Operator, run: _Run
) -> dict[str, Any] | None:
    """Step 2. ``None`` when the start may go on; else the answer that stops the call."""
    try:
        payload = await connector.send(run.target, "GET", run.plan.check_path, operator=operator)
    except httpx.TransportError as exc:
        reason = unreachable_reason(exc)
        run.steps.failed("check_running", f"Cloud Builder did not answer ({reason})")
        return run.answer(
            "not_reachable",
            "Check that Cloud Builder is up, then call this again. Nothing was started.",
            error=f"Cloud Builder did not answer ({reason}); nothing was started",
        )
    except httpx.HTTPStatusError as exc:
        detail = vendor_error_text(exc, run.secrets)
        run.steps.failed("check_running", detail)
        return run.answer(
            "check_failed",
            f"Read {run.plan.status_op} to see the state. Nothing was started.",
            error=f"the check for a running job failed: {detail}; nothing was started",
        )
    run.steps.done("check_running")
    running = run.plan.find_running(payload)
    if running is None:
        return None
    return run.answer(
        "already_running",
        f"A {run.plan.kind} already runs, so nothing was started. Follow it with "
        f"{run.plan.status_op} and this id.",
        job=running,
    )


async def _start(
    connector: CloudBuilderConnector,
    operator: Operator,
    run: _Run,
    path: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    """Step 3. Send the start and say what happened."""
    try:
        payload = await connector.send(
            run.target, run.plan.method, path, operator=operator, json=body
        )
    except httpx.HTTPStatusError as exc:
        detail = vendor_error_text(exc, run.secrets)
        run.steps.failed("start", detail)
        return run.answer(
            "start_failed",
            f"Fix what the error names, then call this again. Read {run.plan.status_op} first.",
            error=f"Cloud Builder refused the start: {detail}",
        )
    except httpx.TransportError as exc:
        reason = unreachable_reason(exc)
        run.steps.unknown("start", f"the start got no answer ({reason})")
        return run.answer(
            "start_unknown",
            f"Do not start again blindly. Read {run.plan.status_op} with no id: a new "
            f"{run.plan.kind} that is running means the start worked.",
            error=f"the start got no answer ({reason}); MEHO does not know if the job runs",
        )
    run.steps.done("start")
    job = {"id": payload.get("id"), "vendor_state": payload.get(run.plan.state_key)}
    return run.answer(
        "accepted",
        f"Cloud Builder accepted the {run.plan.kind} and runs it by itself; it is not done "
        f"yet. Read {run.plan.status_op} with this id until the state is no longer running.",
        job=job,
    )


async def _run_change(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
    plan: _Plan,
) -> dict[str, Any]:
    run = _Run(plan=plan, target=target)
    spec, stop = await _read_secrets(operator, run, params)
    if stop is not None:
        return stop
    connector.remember_secrets(target, run.secrets)
    stop = await _check_running(connector, operator, run)
    if stop is not None:
        return stop
    answer = await _start(connector, operator, run, plan.path, spec)
    if answer["status"] == "accepted" and spec is not None:
        connector.remember_job(target, answer["job"]["id"], run.secrets)
    return answer


async def validation_start_impl(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.cb.validation.start`` — check a bring-up spec on Cloud Builder."""
    plan = _Plan(
        kind="validation",
        status_op=VALIDATION_STATUS_OP_ID,
        check_path=VALIDATIONS_PATH,
        find_running=_running_validation,
        method="POST",
        path=VALIDATIONS_PATH,
        state_key="executionStatus",
    )
    return await _run_change(connector, operator, target, params, plan)


async def sddc_start_impl(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.cb.sddc.start`` — start the bring-up from a validated spec."""
    plan = _Plan(
        kind="bringup",
        status_op=SDDC_STATUS_OP_ID,
        check_path=SDDC_LIST_PATH,
        find_running=_running_bringup,
        method="POST",
        path=SDDC_LIST_PATH,
        state_key="status",
    )
    return await _run_change(connector, operator, target, params, plan)


async def sddc_retry_impl(
    connector: CloudBuilderConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.cb.sddc.retry`` — retry a failed bring-up (optionally with a fixed spec)."""
    path = SDDC_PATH.replace("{id}", str(params["id"]))
    plan = _Plan(
        kind="bringup",
        status_op=SDDC_STATUS_OP_ID,
        check_path=path,
        find_running=_running_self,
        method="PATCH",
        path=path,
        state_key="status",
    )
    return await _run_change(connector, operator, target, params, plan)
