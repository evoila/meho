# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks shared by the fleet certificate and password operations of VCF Operations.

* The version check: the fleet-management API exists on VCF Operations 9.1
  and newer. :func:`require_fleet_api` stops a handler on an older target
  before any vendor call; the approval previews show the same message.
* The secret reference form ``<vault-path>#<field>`` (:func:`parse_password_ref`).
* The plain outcome of a vendor request state (:func:`request_outcome`).
* The approval previews of the two changes. They are registered when the
  connector module is imported (through the handlers module), so they exist
  before any call waits for approval.
"""

from __future__ import annotations

from typing import Any, Final

from packaging.version import InvalidVersion, Version

from meho_backplane.connectors.resolver import resolve_target_version
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = [
    "ENDED_OUTCOMES",
    "FLEET_API_MINIMUM",
    "fleet_version_problem",
    "parse_password_ref",
    "request_outcome",
    "require_fleet_api",
]

#: The first VCF Operations version with the fleet-management API.
FLEET_API_MINIMUM: Final = "9.1"

#: Vendor request states (upper case) and their plain outcome. The vendor
#: file names CREATED, IN_PROGRESS, COMPLETED and FAILED as examples.
_OUTCOME_BY_STATE: Final[dict[str, str]] = {
    "CREATED": "queued",
    "QUEUED": "queued",
    "PENDING": "queued",
    "SUBMITTED": "queued",
    "IN_PROGRESS": "running",
    "INPROGRESS": "running",
    "RUNNING": "running",
    "COMPLETED": "succeeded",
    "SUCCEEDED": "succeeded",
    "SUCCESS": "succeeded",
    "FAILED": "failed",
    "ERROR": "failed",
    "CANCELLED": "canceled",
    "CANCELED": "canceled",
    "ABORTED": "canceled",
}
ENDED_OUTCOMES: Final = frozenset({"succeeded", "failed", "canceled"})


def fleet_version_problem(target: Any, *, op_id: str) -> str | None:
    """Return the "too old" message for *target*, or ``None`` when the call may go on.

    ``None`` too when the version is unknown or does not parse: the vendor's
    own answer is then reported as usual.
    """
    version = resolve_target_version(target)
    if version is None:
        return None
    try:
        too_old = Version(version) < Version(FLEET_API_MINIMUM)
    except InvalidVersion:
        return None
    if not too_old:
        return None
    return (
        f"{op_id}: fleet certificates and passwords need VCF Operations "
        f"{FLEET_API_MINIMUM} or newer; this target is {version}."
    )


def require_fleet_api(target: Any, *, op_id: str) -> None:
    """Raise :exc:`NotImplementedError` on a target older than VCF Operations 9.1.

    The dispatcher turns the error into ``connector_unsupported`` with the
    message in ``extras.detail``.
    """
    problem = fleet_version_problem(target, op_id=op_id)
    if problem is not None:
        raise NotImplementedError(problem)


def request_outcome(state: Any) -> str:
    """The plain outcome of a vendor request state; ``unknown`` when not known."""
    if not isinstance(state, str):
        return "unknown"
    return _OUTCOME_BY_STATE.get(state.strip().upper().replace(" ", "_"), "unknown")


def parse_password_ref(name: str, raw: Any) -> tuple[str, str]:
    """Check a ``"<vault-path>#<field>"`` reference and return ``(path, field)``.

    The message names the parameter, never a value.
    """
    if not isinstance(raw, str) or raw.count("#") != 1:
        raise ValueError(f"{name} must be one '<vault-path>#<field>' reference")
    if any(ch < " " or ch == "\x7f" for ch in raw):
        raise ValueError(f"{name} contains a control character")
    path, _, field = raw.partition("#")
    path, field = path.strip(), field.strip()
    if not path or not field:
        raise ValueError(f"{name} must give a vault path and a field: '<vault-path>#<field>'")
    return path, field


async def _replace_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the approver sees: the certificate, the CA type, the chain size."""
    chain = ctx.params.get("certificateChain")
    preview: dict[str, Any] = {
        "target": getattr(ctx.target, "name", None),
        "certificateId": ctx.params.get("certificateId"),
        "caType": ctx.params.get("caType"),
        "certificate_chain_chars": len(chain) if isinstance(chain, str) else 0,
        "what_else": "The product's services may restart to load the new certificate.",
    }
    problem = fleet_version_problem(ctx.target, op_id=ctx.descriptor.op_id)
    if problem is not None:
        preview["version_problem"] = problem
    return preview


async def _rotate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the approver sees: the account and the two references, never a value."""
    refs: dict[str, str] = {}
    for name in ("current_password_ref", "new_password_ref"):
        try:
            path, field = parse_password_ref(name, ctx.params.get(name))
        except ValueError:
            return None
        refs[name] = f"{path}#{field}"
    preview: dict[str, Any] = {
        "target": getattr(ctx.target, "name", None),
        "passwordAccountKey": ctx.params.get("passwordAccountKey"),
        "userName": ctx.params.get("userName"),
        **refs,
    }
    problem = fleet_version_problem(ctx.target, op_id=ctx.descriptor.op_id)
    if problem is not None:
        preview["version_problem"] = problem
    return preview


register_preview_builder("vrops.fleet.certificate.replace", _replace_preview)
register_preview_builder("vrops.fleet.password.rotate", _rotate_preview)
