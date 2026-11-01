# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks shared by the fleet certificate and password operations of VCF Operations.

* The version check: the fleet-management API exists on VCF Operations 9.1
  and newer, and some filter and input values only on 9.1.1 and newer
  (the value lists, and :data:`ADDED_IN_9_1_1`). :func:`require_fleet_api` stops a handler on an
  older target before any vendor call; the approval previews show the same
  message.
* The secret reference form ``<vault-path>#<field>`` (:func:`parse_password_ref`).
* The plain outcome of a vendor request state (:func:`request_outcome`), and
  its error text. A failed password change can repeat the refused password in
  the vendor's error text, so :func:`request_error_text` and
  :func:`masked_request_payload` drop that text for a password request.
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
    "ADDED_IN_9_1_1",
    "CA_TYPES",
    "CERTIFICATE_APPLIANCES",
    "CERTIFICATE_CATEGORIES",
    "CERTIFICATE_SORT_FIELDS",
    "ENDED_OUTCOMES",
    "FLEET_API_MINIMUM",
    "PASSWORD_APPLIANCES",
    "PASSWORD_ERROR_TEXT_HIDDEN",
    "error_cause_text",
    "fleet_version_problem",
    "masked_request_payload",
    "parse_password_ref",
    "request_error_text",
    "request_outcome",
    "require_fleet_api",
]

#: The first VCF Operations version with the fleet-management API.
FLEET_API_MINIMUM: Final = "9.1"

#: The filter and input values of the op schemas: the union of the vendor's
#: 9.1.0 and 9.1.1 API files, without the vendor's UNKNOWN placeholder. The
#: values the 9.1.1 file added are in :data:`ADDED_IN_9_1_1`; a handler refuses
#: them on an older target before any vendor call. The reconcile lane checks
#: both lists against both files.
_APPLIANCES: Final[list[str]] = [
    "VCENTER",
    "SDDC_MANAGER",
    "NSXT_MANAGER",
    "VCF_AUTOMATION",
    "LOG_MANAGEMENT",
    "VCF_OPERATIONS",
    "VCF_OPS_NETWORK",
    "IDENTITY_BROKER",
    "VCF_OPS_HCX",
    "ESX",
    "VCF_SERVICES_RUNTIME",
    "AVI_LOAD_BALANCER",
    "CLOUD_PROXY",
]
CERTIFICATE_APPLIANCES: Final[list[str]] = [*_APPLIANCES, "LICENSE_SERVER", "SUPERVISOR"]
PASSWORD_APPLIANCES: Final[list[str]] = [*_APPLIANCES, "NSXT_EDGE"]
CA_TYPES: Final[list[str]] = ["VMCA", "OPENSSL_CA", "MSCA", "EXTERNAL_CA", "SELF_SIGNED_CA"]
CERTIFICATE_CATEGORIES: Final[list[str]] = [
    "TLS_CERT",
    "ROOT_CERT",
    "INTERMEDIATE_CERT",
    "STS_CERT",
    "VMCA_ROOT",
    "KMS_CLIENT_CERT",
    "KMS_SERVER_CERT",
    "SOLUTION_USER_CERT",
    "VRNI_GRPC_CERT",
    "VIP",
    "NSX_LB",
    "LOCAL_MANAGER",
    "GLOBAL_MANAGER",
    "CBM_CORFU",
    "CBM_CLUSTER_MANAGER",
    "APH",
    "APH_TN",
    "TN",
    "CCP",
    "WEB_PROXY",
    "SYSLOG_SERVER",
    "COMPUTE_MANAGER",
    "CLIENT_AUTH",
    "POLICY_LB_CERT",
    "POLICY_VPN_CERT",
]
CERTIFICATE_SORT_FIELDS: Final[list[str]] = [
    "STATUS",
    "APPLIANCE",
    "CATEGORY",
    "TYPE",
    "APPLIANCE_FQDN",
    "VCF_COMPONENT_TYPE",
    "SDDC_DOMAIN_ID",
    "VCF_INSTANCE_ENDPOINT",
    "CERTIFICATE_COMMON_NAME",
    "EXPIRY_DATE",
    "AUTO_RENEW_STATUS",
    "CERTIFICATE_PURPOSE",
    "CERTIFICATE_CHAIN_ROLE",
    "CERTIFICATE_CHAIN_GROUP_ID",
]

#: Filter and input values that the vendor's 9.1.1 API file has and the 9.1.0
#: file does not, by parameter name. The schemas accept them; on an older 9.1
#: target the handlers stop before any vendor call. The reconcile lane derives
#: this table from both vendor files.
ADDED_IN_9_1_1: Final[dict[str, frozenset[str]]] = {
    "appliance": frozenset({"CLOUD_PROXY", "LICENSE_SERVER", "SUPERVISOR"}),
    "caType": frozenset({"SELF_SIGNED_CA"}),
    "type": frozenset({"SELF_SIGNED_CA"}),
    "category": frozenset(
        {
            "APH",
            "APH_TN",
            "CBM_CLUSTER_MANAGER",
            "CBM_CORFU",
            "CCP",
            "CLIENT_AUTH",
            "COMPUTE_MANAGER",
            "GLOBAL_MANAGER",
            "KMS_CLIENT_CERT",
            "KMS_SERVER_CERT",
            "LOCAL_MANAGER",
            "NSX_LB",
            "POLICY_LB_CERT",
            "POLICY_VPN_CERT",
            "SOLUTION_USER_CERT",
            "STS_CERT",
            "SYSLOG_SERVER",
            "TN",
            "VIP",
            "VMCA_ROOT",
            "VRNI_GRPC_CERT",
            "WEB_PROXY",
        }
    ),
    "sortBy": frozenset(
        {"CERTIFICATE_CHAIN_GROUP_ID", "CERTIFICATE_CHAIN_ROLE", "CERTIFICATE_PURPOSE"}
    ),
}
_ADDED_IN_VERSION: Final = "9.1.1"

#: Stands in for the vendor's error text of a failed password change.
PASSWORD_ERROR_TEXT_HIDDEN: Final = (
    "The vendor reported an error. MEHO does not show the vendor's text for a "
    "password change, because it can repeat the password."
)
_REDACTED: Final = "***REDACTED***"

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


def fleet_version_problem(
    target: Any, *, op_id: str, params: dict[str, Any] | None = None
) -> str | None:
    """Return the "too old" message for *target*, or ``None`` when the call may go on.

    Too old is a target older than 9.1, or a target older than 9.1.1 when
    *params* holds a value from :data:`ADDED_IN_9_1_1`. ``None`` too when the
    version is unknown or does not parse: the vendor's own answer is then
    reported as usual.
    """
    version = resolve_target_version(target)
    if version is None:
        return None
    try:
        parsed = Version(version)
    except InvalidVersion:
        return None
    if parsed < Version(FLEET_API_MINIMUM):
        return (
            f"{op_id}: fleet certificates and passwords need VCF Operations "
            f"{FLEET_API_MINIMUM} or newer; this target is {version}."
        )
    if parsed >= Version(_ADDED_IN_VERSION):
        return None
    for name, values in ADDED_IN_9_1_1.items():
        value = (params or {}).get(name)
        if isinstance(value, str) and value in values:
            return (
                f"{op_id}: {name} {value} needs VCF Operations {_ADDED_IN_VERSION} "
                f"or newer; this target is {version}."
            )
    return None


def require_fleet_api(target: Any, *, op_id: str, params: dict[str, Any] | None = None) -> None:
    """Raise :exc:`NotImplementedError` when *target* is too old for the call.

    The dispatcher turns the error into ``connector_unsupported`` with the
    message in ``extras.detail``.
    """
    problem = fleet_version_problem(target, op_id=op_id, params=params)
    if problem is not None:
        raise NotImplementedError(problem)


def request_outcome(state: Any) -> str:
    """The plain outcome of a vendor request state; ``unknown`` when not known."""
    if not isinstance(state, str):
        return "unknown"
    return _OUTCOME_BY_STATE.get(state.strip().upper().replace(" ", "_"), "unknown")


def _error_causes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    causes = payload.get("errorCause")
    if not isinstance(causes, list):
        return []
    return [cause for cause in causes if isinstance(cause, dict)]


def _may_echo_a_password(payload: dict[str, Any]) -> bool:
    """True for a password request, and when MEHO cannot tell what the request is.

    The vendor names the kind in ``category`` (``PASSWORD``,
    ``VCF_PASSWORD_MANAGEMENT``, ``VCF_CERTIFICATE_MANAGEMENT``, ...),
    ``requestType`` and ``requestName``. Without any of them the answer is
    treated as a password request, so no text slips through.
    """
    kind = " ".join(
        str(payload.get(key))
        for key in ("category", "requestType", "requestName")
        if payload.get(key)
    )
    return not kind or "PASSWORD" in kind.upper()


def error_cause_text(payload: dict[str, Any]) -> str | None:
    """Join the vendor's ``errorCause`` messages into one text, or ``None``.

    Plain: no password protection. For a workflow request use
    :func:`request_error_text`; for an error answer of the password change the
    handler checks the text against the passwords it sent.
    """
    parts: list[str] = []
    for cause in _error_causes(payload):
        message = cause.get("message")
        detail = cause.get("exceptionMessage")
        text = " — ".join(str(part) for part in (message, detail) if part)
        if text:
            parts.append(text)
    return "; ".join(parts) or None


def request_error_text(payload: dict[str, Any]) -> str | None:
    """The vendor's error messages of a workflow request as one text, or ``None``.

    For a password request the vendor's text is replaced by
    :data:`PASSWORD_ERROR_TEXT_HIDDEN` and the vendor's message ids, because a
    password-policy error can repeat the refused password.
    """
    causes = _error_causes(payload)
    if not causes:
        return None
    if not _may_echo_a_password(payload):
        return error_cause_text(payload)
    ids = [str(cause["messageId"]) for cause in causes if cause.get("messageId")]
    return PASSWORD_ERROR_TEXT_HIDDEN + (f" Vendor message ids: {', '.join(ids)}." if ids else "")


def masked_request_payload(payload: Any) -> Any:
    """*payload* with the error texts of a password request replaced, for the flight recorder.

    ``message`` and ``exceptionMessage`` of each ``errorCause`` become a
    marker; the ids stay. Any other answer comes back as is. No mutation.
    """
    if not isinstance(payload, dict) or not _may_echo_a_password(payload):
        return payload
    causes = payload.get("errorCause")
    if not isinstance(causes, list):
        return payload
    masked = [
        {
            key: _REDACTED if key in ("message", "exceptionMessage") else value
            for key, value in cause.items()
        }
        if isinstance(cause, dict)
        else cause
        for cause in causes
    ]
    return {**payload, "errorCause": masked}


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
    problem = fleet_version_problem(ctx.target, op_id=ctx.descriptor.op_id, params=ctx.params)
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
