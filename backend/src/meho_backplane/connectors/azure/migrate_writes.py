# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure Migrate group and assessment writes (group ``migrate``).

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.migrate_write_ops.MigrateWriteOps`.
Every operation here starts work on the customer's side, so every one of them
waits for a person's approval.

* :func:`group_create_impl` makes a group of machines (a ``PUT`` with the
  caller's name) and can add machines to it.
* :func:`group_update_impl` adds machines to a group and takes machines out.
* :func:`assessment_create_impl` makes an Azure VM assessment (a ``PUT`` with
  the caller's name) and follows it until Azure has computed it.

A retry never makes a second group or assessment: the name is the caller's, so
the same call sends the same ``PUT``. An assessment that already exists with
other settings is refused, not changed.

Once a write has been sent, a later read that fails does not hide it: the
result says the write was sent and what was not read (``poll_error``).

The shared helpers (:func:`arm_write`, :func:`poll_until`) are also used by
:mod:`meho_backplane.connectors.azure.migrate_import`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import (
    AzureApiError,
    AzureNotFoundError,
    AzureRequestError,
    AzureUrlRefusedError,
    clean_azure_text,
)
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paths import (
    MIGRATE_API_VERSION,
    MIGRATE_ASSESSMENT_PATH,
    MIGRATE_ASSESSMENT_WRITE_PATH,
    MIGRATE_GROUP_PATH,
    MIGRATE_GROUP_UPDATE_MACHINES_PATH,
)
from meho_backplane.connectors.azure._polling import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_WAIT_SECONDS,
    operation_reference,
    wait_for_operation,
)
from meho_backplane.connectors.azure.migrate_reads import read_assessment
from meho_backplane.connectors.azure.migrate_rows import (
    assessment_row,
    group_row,
    is_migrate_name,
    migrate_name_param,
    project_params,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "ASSESSMENT_DONE_STATUSES",
    "MAX_MACHINES_PER_CALL",
    "POLL_ERRORS",
    "WriteOutcome",
    "arm_write",
    "assessment_body",
    "assessment_create_impl",
    "group_create_impl",
    "group_update_impl",
    "poll_error_text",
    "poll_until",
]

#: Most machines one group call names. Longer lists are split by the caller.
MAX_MACHINES_PER_CALL: Final[int] = 1000

#: Azure's assessment statuses after which the numbers do not change by themselves.
ASSESSMENT_DONE_STATUSES: Final[frozenset[str]] = frozenset(
    {"completed", "invalid", "outofsync", "outdated", "deleted"}
)

#: How MEHO's words map to Azure's values.
SIZING_VALUES: Final[Mapping[str, str]] = {
    "as_on_premises": "AsOnPremises",
    "performance": "PerformanceBased",
}
PRICING_VALUES: Final[Mapping[str, str]] = {
    "pay_as_you_go": "None",
    "reserved_1_year": "RI1Year",
    "reserved_3_year": "RI3Year",
}

#: Azure's offer code for pay-as-you-go prices.
_PAY_AS_YOU_GO_OFFER: Final[str] = "MSAZR0003P"

#: Clock and sleep. Tests replace them to move time forward.
_monotonic = time.monotonic
_sleep = asyncio.sleep

#: Errors that end a read after a write was sent. The write is not undone and
#: not hidden: the result names the read that failed.
POLL_ERRORS: Final[tuple[type[Exception], ...]] = (
    AzureApiError,
    AzureNotFoundError,
    AzureUrlRefusedError,
    ConnectorAuthError,
    httpx.HTTPError,
)


def poll_error_text(exc: Exception) -> str:
    """MEHO's own text for a read that failed. A lost connection gets a fixed text."""
    if isinstance(exc, httpx.HTTPError):
        return f"Azure did not answer ({type(exc).__name__})."
    return str(clean_azure_text(str(exc)) or type(exc).__name__)


async def poll_until[T](
    read: Callable[[], Awaitable[T]],
    done: Callable[[T], bool],
    *,
    max_seconds: float = DEFAULT_WAIT_SECONDS,
    interval: float = DEFAULT_POLL_SECONDS,
) -> tuple[T, bool]:
    """Call *read* until *done* says yes or *max_seconds* pass.

    Returns the last value and whether it was done. *read* runs at least once.
    A wait that does not fit in the time left is not made.
    """
    started = _monotonic()
    while True:
        value = await read()
        if done(value):
            return value, True
        if _monotonic() - started + interval > max_seconds:
            return value, False
        await _sleep(interval)


@dataclass(frozen=True)
class WriteOutcome:
    """What an Azure write answered.

    ``operation`` is the reference of a long-running operation (to read with
    ``azure.operation.status``), or ``None`` when Azure answered at once.
    ``done`` is false when the operation was still running after MEHO's wait.
    """

    status_code: int
    body: dict[str, Any]
    operation: dict[str, Any] | None
    done: bool


async def arm_write(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    method: str,
    path: str,
    api_version: str,
    body: dict[str, Any],
    *,
    resource_id: str | None,
    repeatable: bool,
) -> WriteOutcome:
    """Send one write. When Azure starts a long-running operation, wait for it.

    *repeatable* says whether the write may be sent again after a lost answer
    or a 5xx: true for a ``PUT`` (same name, same body, same result), false
    for a ``POST``. A failed operation raises :class:`AzureRequestError` with
    Azure's reason.
    """
    response = await send(
        connector,
        target,
        method,
        path,
        operator=operator,
        params={"api-version": api_version},
        json=body,
        repeatable=repeatable,
    )
    payload = json_payload_or_empty(response)
    reference = operation_reference(target, response, resource_id=resource_id)
    if reference is None:
        return WriteOutcome(response.status_code, payload, None, True)
    state = await wait_for_operation(connector, target, reference, operator=operator)
    if state["done"] and state["status"] != "Succeeded":
        error = state["error"] or {}
        raise AzureRequestError(
            f"Azure target {target.name!r}: Azure ended the operation with status "
            f"{state['status']}. Read the group or assessment again to see where it stands.",
            http_status=response.status_code,
            azure_error_code=error.get("code"),
            azure_message=error.get("message"),
            remediation="Read the object with its list operation, then try again.",
        )
    return WriteOutcome(response.status_code, payload, state["operation"], bool(state["done"]))


def _subscription(target: AzureTargetLike) -> str:
    return resolve_target_config(target).subscription_id


def _machine_names(value: object, field: str) -> list[str]:
    """The checked machine names of *value*, in order, without repeats."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of machine names.")
    names: list[str] = []
    for item in value:
        if not is_migrate_name(item):
            raise ValueError(
                f"{field} must hold machine names without < > & : ? / # % \\ or control "
                "characters, as azure.migrate.machine.list returns them (name)."
            )
        if item not in names:
            names.append(item)
    if len(names) > MAX_MACHINES_PER_CALL:
        raise ValueError(
            f"{field} names {len(names)} machines. Send at most {MAX_MACHINES_PER_CALL} "
            "in one call and split a longer list over several calls."
        )
    return names


def _paths(target: AzureTargetLike, template: str, names: Mapping[str, str]) -> str:
    return template.format(subscription_id=_subscription(target), **names)


async def _read_group(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    names: Mapping[str, str],
) -> dict[str, Any]:
    path = _paths(target, MIGRATE_GROUP_PATH, names)
    payload = await connector.arm_get(
        target, path, operator=operator, api_version=MIGRATE_API_VERSION
    )
    return group_row(payload)


async def _update_machines(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    names: Mapping[str, str],
    operation_type: str,
    machines: list[str],
) -> dict[str, Any]:
    """Send one ``updateMachines`` call. Adding a machine twice changes nothing."""
    path = _paths(target, MIGRATE_GROUP_UPDATE_MACHINES_PATH, names)
    outcome = await arm_write(
        connector,
        operator,
        target,
        "POST",
        path,
        MIGRATE_API_VERSION,
        {"properties": {"operationType": operation_type, "machines": machines}},
        resource_id=_paths(target, MIGRATE_GROUP_PATH, names),
        # Adding a machine that is in the group, or taking out one that is
        # not, changes nothing, so the call may be sent again.
        repeatable=True,
    )
    return {
        "operation_type": operation_type,
        "machines": len(machines),
        "done": outcome.done,
        "operation": outcome.operation,
    }


async def _step(
    step: Awaitable[dict[str, Any]], target: AzureTargetLike, *, done: str
) -> dict[str, Any]:
    """Run a later step of a multi-step write. A failure says which steps already ran."""
    try:
        return await step
    except POLL_ERRORS as exc:
        raise AzureRequestError(
            f"Azure target {target.name!r}: a step failed after an earlier one ran. {done} "
            f"The failed step: {poll_error_text(exc)} Send the same call again: it makes no "
            "second group and changes nothing twice.",
            http_status=getattr(exc, "http_status", None) or 502,
            azure_error_code=getattr(exc, "azure_error_code", None),
            azure_message=getattr(exc, "azure_message", None),
            remediation=(
                "Send the same call again, or read the group with azure.migrate.group.list."
            ),
        ) from exc


async def _group_result(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    names: Mapping[str, str],
    steps: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    """The answer of a group write: the steps that ran and the group as Azure reads it now."""
    group: dict[str, Any] | None = None
    poll_error: str | None = None
    try:
        group = await _read_group(connector, operator, target, names)
    except POLL_ERRORS as exc:
        poll_error = f"The group was changed but could not be read back: {poll_error_text(exc)}"
    return {"steps": steps, "group": group, "poll_error": poll_error, **extra}


async def group_create_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.group.create``: make a group, then add the machines named.

    The ``PUT`` carries the caller's group name, so the same call again makes
    no second group. The machines are added with ``updateMachines``, which
    changes nothing for a machine that is already in the group.
    """
    names = {**project_params(params), "group": migrate_name_param(params, "group")}
    machines = _machine_names(params.get("machines"), "machines")
    group_type = params.get("group_type", "Default")
    if group_type not in ("Default", "Import"):
        raise ValueError("group_type must be Default or Import.")
    steps: list[dict[str, Any]] = []
    created = await arm_write(
        connector,
        operator,
        target,
        "PUT",
        _paths(target, MIGRATE_GROUP_PATH, names),
        MIGRATE_API_VERSION,
        {"properties": {"groupType": group_type}},
        resource_id=_paths(target, MIGRATE_GROUP_PATH, names),
        repeatable=True,
    )
    steps.append({"step": "group_put", "done": created.done, "operation": created.operation})
    if machines:
        steps.append(
            await _step(
                _update_machines(connector, operator, target, names, "Add", machines),
                target,
                done="The group was made.",
            )
        )
    return await _group_result(connector, operator, target, names, steps)


async def group_update_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.group.update``: add machines to a group and take machines out.

    ``add`` runs first, then ``remove``. A machine named in both lists is
    refused, because the result would depend on the order.
    """
    names = {**project_params(params), "group": migrate_name_param(params, "group")}
    add = _machine_names(params.get("add"), "add")
    remove = _machine_names(params.get("remove"), "remove")
    if not add and not remove:
        raise ValueError("Name at least one machine in add or remove.")
    both = [name for name in add if name in remove]
    if both:
        raise ValueError("A machine cannot be in both add and remove.")
    steps: list[dict[str, Any]] = []
    if add:
        steps.append(await _update_machines(connector, operator, target, names, "Add", add))
    if remove:
        steps.append(
            await _step(
                _update_machines(connector, operator, target, names, "Remove", remove),
                target,
                done="The machines were added." if add else "Nothing was changed before.",
            )
        )
    return await _group_result(connector, operator, target, names, steps)


# ----------------------------------------------------------------------
# Assessment
# ----------------------------------------------------------------------


def assessment_body(params: Mapping[str, Any]) -> dict[str, Any]:
    """The ``PUT`` body of an Azure VM assessment, from the checked parameters."""
    sizing = SIZING_VALUES[params["sizing"]]
    pricing = PRICING_VALUES[params.get("pricing", "pay_as_you_go")]
    return {
        "properties": {
            "azureLocation": params["region"],
            "currency": params.get("currency", "EUR"),
            "sizingCriterion": sizing,
            "reservedInstance": pricing,
            "azureHybridUseBenefit": "Yes" if params.get("hybrid_benefit") else "No",
            "scalingFactor": float(params.get("comfort_factor", 1.0)),
            "azurePricingTier": "Standard",
            "azureOfferCode": _PAY_AS_YOU_GO_OFFER,
            "percentile": "Percentile95",
            "timeRange": "Day",
        }
    }


#: The parameter behind a setting of the body, for the error text. Other settings are fixed.
_SETTING_NAMES: Final[Mapping[str, str]] = {
    "azureLocation": "region",
    "currency": "currency",
    "sizingCriterion": "sizing",
    "reservedInstance": "pricing",
    "azureHybridUseBenefit": "hybrid_benefit",
    "scalingFactor": "comfort_factor",
}


def _same_setting(have: object, want: object) -> bool:
    if isinstance(want, float):
        return (
            isinstance(have, int | float)
            and not isinstance(have, bool)
            and abs(float(have) - want) <= 1e-6
        )
    return isinstance(have, str) and have.casefold() == str(want).casefold()


def _differences(existing: Mapping[str, Any], body: Mapping[str, Any]) -> list[str]:
    """The settings of an existing assessment that differ from the wanted body.

    Every setting the body sends is compared with Azure's own field, so an
    assessment with another offer, percentile or time range never counts
    as the same.
    """
    raw = existing.get("properties")
    have: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return [
        _SETTING_NAMES.get(field, field)
        for field, want in body["properties"].items()
        if not _same_setting(have.get(field), want)
    ]


async def _existing_assessment(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    names: Mapping[str, str],
) -> dict[str, Any] | None:
    """Azure's own answer for the assessment, or ``None`` when there is none."""
    try:
        return await connector.arm_get(
            target,
            _paths(target, MIGRATE_ASSESSMENT_PATH, names),
            operator=operator,
            api_version=MIGRATE_API_VERSION,
        )
    except AzureNotFoundError:
        return None


def _is_done(row: Mapping[str, Any]) -> bool:
    status = row.get("status")
    return isinstance(status, str) and status.casefold() in ASSESSMENT_DONE_STATUSES


async def assessment_create_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.assessment.create``: make an Azure VM assessment and follow it.

    An assessment of this name that already has the same settings is not sent
    again: its computation is followed. One with other settings is refused,
    so a retry or a second caller never changes an assessment someone else
    reads. Reads the assessment until Azure says it is computed, or until
    MEHO's wait is over (``done`` is false then).
    """
    names = {
        **project_params(params),
        "group": migrate_name_param(params, "group"),
        "assessment": migrate_name_param(params, "assessment"),
    }
    body = assessment_body(params)
    existing = await _existing_assessment(connector, operator, target, names)
    state = "existing"
    operation: dict[str, Any] | None = None
    if existing is None:
        state = "created"
        outcome = await arm_write(
            connector,
            operator,
            target,
            "PUT",
            _paths(target, MIGRATE_ASSESSMENT_WRITE_PATH, names),
            MIGRATE_API_VERSION,
            body,
            resource_id=_paths(target, MIGRATE_ASSESSMENT_PATH, names),
            repeatable=True,
        )
        operation = outcome.operation
    else:
        differs = _differences(existing, body)
        if differs:
            raise ValueError(
                f"Assessment {names['assessment']!r} already exists in group "
                f"{names['group']!r} with other settings ({', '.join(differs)}). MEHO did "
                "not change it. Use a new assessment name."
            )
    row = None if existing is None else assessment_row(existing, group=names["group"])
    poll_error: str | None = None
    done = False
    try:
        polled, done = await poll_until(
            lambda: read_assessment(connector, operator, target, names), _is_done
        )
        row = polled
    except POLL_ERRORS as exc:
        poll_error = (
            f"The assessment was sent but could not be read: {poll_error_text(exc)} "
            "Call azure.migrate.assessment.list to see where it stands."
        )
    return {
        "state": state,
        "done": done,
        "status": row.get("status") if row else None,
        "machine_count": row.get("machine_count") if row else None,
        "readiness_summary": row.get("readiness_summary") if row else {},
        "assessment": row,
        "operation": operation,
        "poll_error": poll_error,
    }
