# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Body of ``azure.patching.assessment.start``: check one machine for missing updates.

The operation asks Azure to check one Azure VM or one Arc machine for missing
updates (``assessPatches``) and follows Azure's long-running operation to its
end:

1. ``machine_id`` is checked before any call: an Azure VM or an Arc machine,
   in the target's own subscription, with names that follow Azure's rules. A
   machine of another subscription is refused and nothing is sent.
2. MEHO sends one ``POST``. Like every write, it is sent again only after a
   429 or a 401 (then Azure did not run it), never after a timeout or a 5xx
   (:func:`~meho_backplane.connectors.azure._http.send`).
3. Azure answers 202. Both API descriptions say the result is at the
   ``Location`` URL (``final-state-via: location``), so MEHO polls that URL
   with :func:`~meho_backplane.connectors.azure._polling.wait_for_operation`:
   202 while the check runs, 200 with the result when it is done. Without a
   ``Location`` header MEHO falls back to the usual poll URL
   (:func:`~meho_backplane.connectors.azure._polling.operation_reference`).
4. When the wait runs out, or a poll fails, the answer has ``done: false`` and
   the operation reference. ``azure.operation.status`` reads the outcome
   later from that reference, also after a MEHO restart, without sending the
   check again.

A check installs no updates. The first check on a machine makes Azure add its
update extension to the machine. A second check is a new check: it changes
nothing on the machine and only gives fresh counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureApiError, AzureNotFoundError
from meho_backplane.connectors.azure._http import retry_after_seconds, send
from meho_backplane.connectors.azure._paths import (
    ARC_API_VERSION,
    ARC_ASSESS_PATCHES_PATH,
    VM_ASSESS_API_VERSION,
    VM_ASSESS_PATCHES_PATH,
)
from meho_backplane.connectors.azure._polling import (
    OperationReference,
    check_poll_url,
    operation_reference,
    wait_for_operation,
)
from meho_backplane.connectors.azure.arc_rows import Row, assessment_result
from meho_backplane.connectors.azure.arc_schemas import (
    MACHINE_ID_MAX_LENGTH,
    RESOURCE_GROUP_CHARS,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = ["CheckedMachine", "checked_machine", "patching_assessment_start_impl"]

#: The machine types the check runs on: (provider, type) in lower case, to
#: (the type as Azure writes it, the path, the API version).
_MACHINE_TYPES: Final[dict[tuple[str, str], tuple[str, str, str]]] = {
    ("microsoft.compute", "virtualmachines"): (
        "Microsoft.Compute/virtualMachines",
        VM_ASSESS_PATCHES_PATH,
        VM_ASSESS_API_VERSION,
    ),
    ("microsoft.hybridcompute", "machines"): (
        "Microsoft.HybridCompute/machines",
        ARC_ASSESS_PATCHES_PATH,
        ARC_API_VERSION,
    ),
}

#: A VM or Arc machine name: letters, digits, ``_``, ``-`` and ``.``, at most
#: 80 characters, not starting with ``.`` (so never ``.`` or ``..``).
_MACHINE_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}")
_RESOURCE_GROUP: Final[re.Pattern[str]] = re.compile(RESOURCE_GROUP_CHARS)

#: Errors of a poll after which MEHO returns the reference instead of failing:
#: the check was accepted and may still run.
_POLL_ERRORS: Final[tuple[type[Exception], ...]] = (
    AzureApiError,
    AzureNotFoundError,
    httpx.HTTPError,
)


@dataclass(frozen=True)
class CheckedMachine:
    """A machine ID that passed :func:`checked_machine`."""

    machine_type: str
    resource_id: str
    path: str
    api_version: str


def checked_machine(target: AzureTargetLike, machine_id: object) -> CheckedMachine:
    """Check *machine_id* and return the machine, its path and its API version.

    Allowed: ``/subscriptions/<the target's subscription>/resourceGroups/<group>/
    providers/Microsoft.Compute/virtualMachines/<name>`` or the same with
    ``Microsoft.HybridCompute/machines``, in any letter case. Refused: another
    subscription, another resource type, a longer or shorter path, and names
    that break Azure's rules (``..``, ``?``, ``#``, ``%``, ...). Raises
    ``ValueError`` before any call to Azure; the message never repeats the ID.
    """
    config = resolve_target_config(target)
    text = machine_id if isinstance(machine_id, str) else ""
    parts = text.split("/")
    known = (
        len(text) <= MACHINE_ID_MAX_LENGTH
        and len(parts) == 9
        and parts[0] == ""
        and parts[1].lower() == "subscriptions"
        and parts[3].lower() == "resourcegroups"
        and parts[5].lower() == "providers"
        and (parts[6].lower(), parts[7].lower()) in _MACHINE_TYPES
        and _RESOURCE_GROUP.fullmatch(parts[4]) is not None
        and _MACHINE_NAME.fullmatch(parts[8]) is not None
    )
    if not known:
        raise ValueError(
            f"Azure target {target.name!r}: machine_id must be the ID of one Azure VM "
            "(.../providers/Microsoft.Compute/virtualMachines/<name>) or one Arc machine "
            "(.../providers/Microsoft.HybridCompute/machines/<name>). MEHO sent nothing "
            "to Azure."
        )
    if parts[2].lower() != config.subscription_id:
        raise ValueError(
            f"Azure target {target.name!r}: machine_id is in another subscription than "
            "this target. Use the target of that subscription. MEHO sent nothing to Azure."
        )
    machine_type, template, api_version = _MACHINE_TYPES[(parts[6].lower(), parts[7].lower())]
    group, name = parts[4], parts[8]
    return CheckedMachine(
        machine_type=machine_type,
        resource_id=(
            f"/subscriptions/{config.subscription_id}/resourceGroups/{group}"
            + f"/providers/{machine_type}/{name}"
        ),
        path=template.format(
            subscription_id=config.subscription_id,
            resource_group=quote(group, safe=""),
            name=quote(name, safe=""),
        ),
        api_version=api_version,
    )


def _reference(
    target: AzureTargetLike, response: httpx.Response, resource_id: str
) -> OperationReference | None:
    """The poll reference of a 201 or 202 answer: ``Location`` first, the result is there."""
    location = response.headers.get("location")
    if response.status_code in (201, 202) and location:
        check_poll_url(target, location)
        return OperationReference(
            poll_url=location,
            poll_kind="location",
            resource_id=resource_id,
            started_at=datetime.now(UTC).isoformat(),
            retry_after_seconds=retry_after_seconds(response),
        )
    return operation_reference(target, response, resource_id=resource_id)


def _answer(machine: CheckedMachine, outcome: dict[str, Any]) -> Row:
    """The operation's answer from the state of Azure's operation."""
    ended = outcome.get("done") is True and outcome.get("status") == "Succeeded"
    return {
        "machine_id": machine.resource_id,
        "machine_type": machine.machine_type,
        "status": outcome.get("status"),
        "done": outcome.get("done") is True,
        "assessment": assessment_result(outcome.get("result")) if ended else None,
        "error": outcome.get("error"),
        "retry_after_seconds": outcome.get("retry_after_seconds"),
        "operation": outcome.get("operation"),
    }


def _poll_failed(reference: OperationReference, exc: Exception) -> dict[str, Any]:
    """The state when a poll failed: unknown, with the reference to read it later."""
    return {
        "status": "Unknown",
        "done": False,
        "error": {
            "code": type(exc).__name__,
            "message": (
                "MEHO could not read the state of the check. Azure accepted it, so it may "
                "still run. Read it later with azure.operation.status and this operation."
            ),
        },
        "result": None,
        "retry_after_seconds": getattr(exc, "retry_after_seconds", None),
        "operation": reference.as_dict(),
    }


async def patching_assessment_start_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.patching.assessment.start``: check one VM or Arc machine for missing updates."""
    machine = checked_machine(target, params.get("machine_id"))
    response = await send(
        connector,
        target,
        "POST",
        machine.path,
        operator=operator,
        params={"api-version": machine.api_version},
    )
    reference = _reference(target, response, machine.resource_id)
    if reference is None:
        # Azure answered without a URL to poll: with the result (200), or not at all.
        body = json_payload_or_empty(response)
        ended = response.status_code == 200
        return _answer(
            machine,
            {
                "status": "Succeeded" if ended else "Accepted",
                "done": ended,
                "error": None,
                "result": body or None,
                "retry_after_seconds": None,
                "operation": None,
            },
        )
    try:
        outcome = await wait_for_operation(connector, target, reference, operator=operator)
    except _POLL_ERRORS as exc:
        outcome = _poll_failed(reference, exc)
    return _answer(machine, outcome)
