# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``installer.sddc.resources.calculate`` — the CPU, memory and storage a deploy spec needs.

VCF Installer 9.1 and newer only (the call is not in the 9.0 API file). The
handler does the whole work inside its own call:

1. Stop on a 9.0 target before any vendor call (``NotImplementedError``,
   which the dispatcher turns into an audited ``connector_unsupported``).
2. Build the spec: refuse a spec that holds a password value, then put each
   password the caller names in ``password_refs`` (a Vault path per JSON
   pointer) into a copy of the spec.
3. ``POST /v1/sddcs/resources-calculation`` with the spec. The appliance
   answers ``202`` with a ``CapacityValidation`` and its id.
4. Read ``GET /v1/sddcs/resources-calculation/{id}`` every few seconds until
   ``executionStatus`` is no longer ``IN_PROGRESS``, or until
   ``timeout_seconds`` has passed.
5. Return the required and the available capacity and the checks as a flat
   list of rows.

The calculation changes nothing on the appliance or the hosts. MEHO opens
nothing it must clean up (no download session, no temporary item): the
login token is the connector's shared one. If MEHO stops during the call,
the vendor's calculation record stays on the appliance; it is harmless and a
new run makes a new one.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.vcf_installer._secret_refs import hide_secret, read_secret_ref
from meho_backplane.connectors.vcf_installer._version_gate import require_installer_version

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "INSTALLER_RESOURCES_CALCULATE_OP_ID",
    "RESOURCES_CALCULATION_MIN_VERSION",
    "RESOURCES_DECLARED_OP_IDS",
    "installer_resources_calculate_impl",
]

INSTALLER_RESOURCES_CALCULATE_OP_ID: Final = "installer.sddc.resources.calculate"
#: The first Installer version that has the resource calculation.
RESOURCES_CALCULATION_MIN_VERSION: Final = "9.1"

_CALCULATION_PATH: Final = "/v1/sddcs/resources-calculation"
_CALCULATION_STATUS_PATH: Final = "/v1/sddcs/resources-calculation/{id}"

#: Every ``METHOD:/path`` this module calls. The reconcile lane checks them
#: against the pinned 9.1 installer file (only 9.1 has them).
RESOURCES_DECLARED_OP_IDS: frozenset[str] = frozenset(
    {f"POST:{_CALCULATION_PATH}", f"GET:{_CALCULATION_STATUS_PATH}"}
)

#: Seconds between two reads of the calculation. Tests set it to 0.
POLL_INTERVAL_SECONDS: float = 5.0
DEFAULT_TIMEOUT_SECONDS: Final = 600


class InlineSecretError(ValueError):
    """The spec holds a password value instead of a Vault reference."""


def _inline_secret_paths(node: Any, path: str = "") -> list[str]:
    """JSON pointers of every secret-named field that holds a value."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            pointer = f"{path}/{key}"
            if is_secret_param_name(str(key)) and isinstance(value, str) and value:
                found.append(pointer)
            else:
                found.extend(_inline_secret_paths(value, pointer))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_inline_secret_paths(value, f"{path}/{index}"))
    return found


def _set_pointer(doc: dict[str, Any], pointer: str, value: str) -> None:
    """Put *value* at the JSON *pointer* in *doc*; missing objects on the way are made."""
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    if not parts:
        raise ValueError(f"password_refs pointer {pointer!r} names no field")
    node: Any = doc
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
    if isinstance(node, list):
        node[int(parts[-1])] = value
    else:
        node[parts[-1]] = value


async def _spec_with_passwords(
    operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """A copy of the spec with each referenced password put in, and the passwords."""
    inline = _inline_secret_paths(params["spec"])
    if inline:
        raise InlineSecretError(
            "the spec holds a password value at "
            + ", ".join(inline)
            + ". Remove it and name its Vault path in password_refs. Nothing was sent."
        )
    spec = copy.deepcopy(params["spec"])
    secrets: list[str] = []
    for index, ref in enumerate(params.get("password_refs") or []):
        password = await read_secret_ref(
            operator, target, ref, param=f"password_refs[{index}].secret_ref"
        )
        try:
            _set_pointer(spec, str(ref["pointer"]), password)
        except (IndexError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError(
                f"password_refs[{index}].pointer {ref['pointer']!r} does not fit the spec"
            ) from exc
        secrets.append(password)
    return spec, tuple(secrets)


def _flat_checks(checks: Any, parent: str | None = None) -> list[dict[str, Any]]:
    """One row per check, nested checks included, with the vendor error text."""
    rows: list[dict[str, Any]] = []
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, dict):
            continue
        error = check.get("errorResponse")
        rows.append(
            {
                "description": check.get("description"),
                "severity": check.get("severity"),
                "result_status": check.get("resultStatus"),
                "error": error.get("message") if isinstance(error, dict) else None,
                "parent": parent,
            }
        )
        rows.extend(_flat_checks(check.get("nestedValidationChecks"), check.get("description")))
    return rows


def _answer(calculation: dict[str, Any], status: str, secrets: tuple[str, ...]) -> dict[str, Any]:
    checks = _flat_checks(calculation.get("validationChecks"))
    for row in checks:
        row["error"] = hide_secret(row["error"], secrets)
    return {
        "status": status,
        "id": calculation.get("id"),
        "execution_status": calculation.get("executionStatus"),
        "result_status": calculation.get("resultStatus"),
        "required": calculation.get("requiredCapacity"),
        "available": calculation.get("availableCapacity"),
        "checks": checks,
    }


async def installer_resources_calculate_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run the resource calculation to its end and return the result check by check.

    ``status`` is ``done`` when the calculation finished, ``timed_out`` when
    ``timeout_seconds`` passed first (the answer then has the id and the last
    state), ``read_failed`` when a read of the calculation failed part-way
    (the answer then has the id, the last state and the error), and
    ``rejected`` when the appliance refused the spec (for example ``400`` for
    a spec that is not valid).
    """
    require_installer_version(
        target,
        op_id=INSTALLER_RESOURCES_CALCULATE_OP_ID,
        minimum=RESOURCES_CALCULATION_MIN_VERSION,
        feature="the resource calculation",
    )
    spec, secrets = await _spec_with_passwords(operator, target, params)
    try:
        calculation = await connector._post_json_with_session_retry(
            target, _CALCULATION_PATH, operator=operator, json=spec
        )
    except httpx.HTTPStatusError as exc:
        try:
            body = exc.response.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        return {
            "status": "rejected",
            "http_status": exc.response.status_code,
            "error_code": body.get("errorCode"),
            "message": hide_secret(body.get("message") or exc.response.reason_phrase, secrets),
        }
    calc_id = str(calculation.get("id") or "")
    deadline = time.monotonic() + float(params.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    while calc_id and calculation.get("executionStatus") == "IN_PROGRESS":
        if time.monotonic() >= deadline:
            return _answer(calculation, "timed_out", secrets)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            calculation = await connector._get_json_with_session_retry(
                target, _CALCULATION_STATUS_PATH.replace("{id}", calc_id), operator=operator
            )
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            answer = _answer(calculation, "read_failed", secrets)
            answer["error"] = _read_error(exc)
            return answer
    return _answer(calculation, "done", secrets)


def _read_error(exc: httpx.HTTPError) -> str:
    """A short text for a failed read of the calculation (never the request body)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} while reading the calculation"
    return f"no answer from the appliance ({type(exc).__name__}) while reading the calculation"
