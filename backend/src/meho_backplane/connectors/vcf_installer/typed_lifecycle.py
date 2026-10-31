# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers for the Installer reuse checks, the cancel, and the appliance reads.

Seven typed operations, each a thin bound-method shim on
:class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
(metadata in :mod:`.typed_ops_lifecycle`):

* ``installer.discovery.vcenter`` / ``.sddc_manager`` / ``.vcf_operations`` —
  ``POST /v1/sddcs/{vcenter,sddcm,vcfops}-discovery``. The Installer logs in
  to an existing system and reports what it found, so a bring-up can reuse
  it. Nothing changes. The login of that system comes from a Vault secret the
  call names (``secret_ref``), read under the caller's identity; it is never a
  parameter.
* ``installer.sddc.cancel`` — ``DELETE /v1/tasks/{id}`` (the vendor
  ``cancelTask``). Only an ``IN_PROGRESS`` task can be cancelled; the vendor
  answers ``409`` otherwise, and that error reaches the caller as it is.
* ``installer.appliance.info`` — ``GET /v1/system/appliance-info``.
* ``installer.release.list`` — ``GET /v1/releases``, one flat row per release.
* ``installer.sddc.spec.get`` — ``GET /v1/sddcs/{id}/spec``, with every
  secret-named field replaced before the answer leaves the connector.

**Version check.** SDDC Manager discovery exists only from Installer 9.1 on;
the pinned 9.0 API file has no such path. :func:`require_installer_version`
stops that call on an older target before any Vault read or vendor call. It
raises :exc:`NotImplementedError`, which the dispatcher turns into an audited
``connector_unsupported`` answer.

**Path ids.** The cancel and the spec read put a caller-given id into the
vendor path. :func:`path_id` takes only one plain path segment (letters,
digits, ``.``, ``_``, ``~``, ``-``; not ``.`` or ``..``), so an id can never
change which vendor endpoint the call reaches. The same rule is the
``pattern`` of both ``id`` params, so the dispatcher refuses a bad id as
``invalid_params`` before any approval is asked for; the handler checks it
again before it builds the URL.

**Secret hygiene of the discovery calls.** The request body holds the other
system's password. On a vendor error the handler raises a copy of the
:class:`httpx.HTTPStatusError` that has no request body and keeps only the
vendor's ``errorCode`` / ``message`` / ``remediationMessage`` — and drops even
those when the raw answer holds the password. The copy keeps the status and
the request headers, so a ``401`` still drives the dispatcher's
re-login-once path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx
from packaging.version import InvalidVersion, Version

from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.resolver import resolve_target_version
from meho_backplane.redaction.secret_fields import scrub_secret_fields

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "DISCOVERY_SDDC_MANAGER_OP_ID",
    "DISCOVERY_VCENTER_OP_ID",
    "DISCOVERY_VCF_OPERATIONS_OP_ID",
    "INSTALLER_SDDC_CANCEL_OP_ID",
    "INSTALLER_SDDC_SPEC_GET_OP_ID",
    "LIFECYCLE_9_1_ONLY_OP_IDS",
    "LIFECYCLE_DECLARED_OP_IDS",
    "PATH_ID_PATTERN",
    "RELEASE_ROW_KEYS",
    "DiscoverySecretError",
    "installer_appliance_info_impl",
    "installer_discovery_sddcm_impl",
    "installer_discovery_vcenter_impl",
    "installer_discovery_vcfops_impl",
    "installer_release_list_impl",
    "installer_sddc_cancel_impl",
    "installer_sddc_spec_get_impl",
    "path_id",
    "require_installer_version",
]

DISCOVERY_VCENTER_OP_ID: Final = "installer.discovery.vcenter"
DISCOVERY_SDDC_MANAGER_OP_ID: Final = "installer.discovery.sddc_manager"
DISCOVERY_VCF_OPERATIONS_OP_ID: Final = "installer.discovery.vcf_operations"
INSTALLER_SDDC_CANCEL_OP_ID: Final = "installer.sddc.cancel"
INSTALLER_SDDC_SPEC_GET_OP_ID: Final = "installer.sddc.spec.get"

# --- Vendor paths (the spec-reconcile lane reads LIFECYCLE_DECLARED_OP_IDS). ---
_VCENTER_DISCOVERY_PATH: Final = "/v1/sddcs/vcenter-discovery"
_SDDC_MANAGER_DISCOVERY_PATH: Final = "/v1/sddcs/sddcm-discovery"
_VCF_OPERATIONS_DISCOVERY_PATH: Final = "/v1/sddcs/vcfops-discovery"
_TASK_PATH: Final = "/v1/tasks/{id}"
_APPLIANCE_INFO_PATH: Final = "/v1/system/appliance-info"
_RELEASES_PATH: Final = "/v1/releases"
_SDDC_SPEC_PATH: Final = "/v1/sddcs/{id}/spec"

#: Every ``METHOD:/path`` this module sends. The reconcile lane checks each
#: against the pinned 9.1 file, and each not in
#: :data:`LIFECYCLE_9_1_ONLY_OP_IDS` against the pinned 9.0 file too.
LIFECYCLE_DECLARED_OP_IDS: frozenset[str] = frozenset(
    {
        f"POST:{_VCENTER_DISCOVERY_PATH}",
        f"POST:{_SDDC_MANAGER_DISCOVERY_PATH}",
        f"POST:{_VCF_OPERATIONS_DISCOVERY_PATH}",
        f"DELETE:{_TASK_PATH}",
        f"GET:{_APPLIANCE_INFO_PATH}",
        f"GET:{_RELEASES_PATH}",
        f"GET:{_SDDC_SPEC_PATH}",
    }
)

#: The calls that the pinned 9.0 file does not have. Each one's op checks
#: the target version first (:func:`require_installer_version`).
LIFECYCLE_9_1_ONLY_OP_IDS: frozenset[str] = frozenset({f"POST:{_SDDC_MANAGER_DISCOVERY_PATH}"})

#: The scalar ``Release`` fields each ``installer.release.list`` row keeps
#: (vendor ``components.schemas.Release``). ``bom`` is added as a short
#: ``{component name: version}`` map; the long ``description``, ``sku``,
#: ``updates`` and ``patchBundles`` stay out of the row.
RELEASE_ROW_KEYS: tuple[str, ...] = (
    "product",
    "version",
    "releaseDate",
    "minInstallerVersion",
    "minCompatibleVcfVersion",
    "eol",
)

#: Op param name → vendor query param name of ``GET /v1/releases``.
_RELEASE_QUERY: Final = {
    "version_eq": "versionEq",
    "version_ge": "versionGe",
    "include_only_compatible": "includeOnlyCompatible",
}

#: The vendor error fields a discovery error keeps.
_KEPT_ERROR_FIELDS: Final = ("errorCode", "message", "remediationMessage")

#: One plain URL path segment: the RFC 3986 unreserved characters, and not
#: the dot segments ``.`` / ``..``. Used as the ``pattern`` of every ``id``
#: param this module puts into a vendor path, and checked again by
#: :func:`path_id`.
PATH_ID_PATTERN: Final = r"^(?!\.{1,2}$)[A-Za-z0-9._~-]+$"
_PATH_ID_RE: Final = re.compile(PATH_ID_PATTERN)


class DiscoverySecretError(Exception):
    """The Vault secret a discovery call names has no usable login.

    The message names the path and the missing key, never a value.
    """


def path_id(value: Any, *, op_id: str) -> str:
    """*value* as one plain path segment, or :exc:`ValueError`.

    Refuses a slash, a backslash, ``?``, ``#``, ``%``, whitespace, control
    characters, and the dot segments ``.`` and ``..`` — anything that could
    make the built URL reach another vendor endpoint than the one the op
    names. The message repeats the id, so the caller sees what was refused.
    """
    text = str(value)
    if not _PATH_ID_RE.fullmatch(text):
        raise ValueError(
            f"{op_id}: id={text!r} is not a plain id. Allowed: letters, digits, "
            "'.', '_', '~' and '-' (and not '.' or '..'). Nothing was sent."
        )
    return text


def require_installer_version(target: Any, *, op_id: str, minimum: str, feature: str) -> None:
    """Raise :exc:`NotImplementedError` when *target* runs an Installer older than *minimum*.

    The version comes from
    :func:`~meho_backplane.connectors.resolver.resolve_target_version` (the
    probed version first, then the one the operator set). An unknown or
    unparseable version goes on; the vendor's own answer then decides. The
    message names the op, the target's version and the versions that have
    the call.
    """
    version = resolve_target_version(target)
    if version is None:
        return
    try:
        too_old = Version(version) < Version(minimum)
    except InvalidVersion:
        return
    if too_old:
        raise NotImplementedError(
            f"{op_id}: {feature} needs VCF Installer {minimum} or newer; this target is {version}."
        )


@dataclass(frozen=True)
class _Discovery:
    """What differs between the three discovery calls."""

    op_id: str
    path: str
    feature: str
    username_field: str
    password_field: str
    username_required: bool
    minimum: str | None = None


_VCENTER: Final = _Discovery(
    op_id=DISCOVERY_VCENTER_OP_ID,
    path=_VCENTER_DISCOVERY_PATH,
    feature="vCenter discovery",
    username_field="username",
    password_field="password",
    username_required=True,
)
_SDDC_MANAGER: Final = _Discovery(
    op_id=DISCOVERY_SDDC_MANAGER_OP_ID,
    path=_SDDC_MANAGER_DISCOVERY_PATH,
    feature="SDDC Manager discovery",
    username_field="adminUsername",
    password_field="adminPassword",
    username_required=True,
    minimum="9.1",
)
_VCF_OPERATIONS: Final = _Discovery(
    op_id=DISCOVERY_VCF_OPERATIONS_OP_ID,
    path=_VCF_OPERATIONS_DISCOVERY_PATH,
    feature="VCF Operations discovery",
    username_field="adminUsername",
    password_field="adminPassword",
    username_required=False,
)


@dataclass(slots=True)
class _SecretRefTarget:
    """Points the shared Vault read at the call's ``secret_ref``.

    Carries the target's name and host for log lines; the ref is the call's,
    not the Installer's own login secret.
    """

    name: str
    host: str
    secret_ref: str | None


async def _read_login(
    operator: Operator, target: InstallerTargetLike, params: dict[str, Any], kind: _Discovery
) -> tuple[str, str]:
    """``(username, password)`` from the Vault secret at ``params["secret_ref"]``.

    A Vault refusal or a missing path raises
    :class:`~meho_backplane.connectors._shared.vault_creds.SecretRefReadError`
    (the dispatcher answers ``vault_credentials_read_error``). A secret
    without a password — or without a username where the vendor needs one —
    raises :class:`DiscoverySecretError`. The username may be empty only for
    VCF Operations, where the vendor marks it optional.
    """
    secret_ref = str(params["secret_ref"]).strip()
    mount = str(params.get("secret_mount") or DEFAULT_KV_MOUNT).strip()
    data = await load_param_secret_data(
        _SecretRefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
        operator,
        param="secret_ref",
        mount=mount,
    )
    values = {
        key: strip_credential_value(data[key]) if isinstance(data.get(key), str) else ""
        for key in ("username", "password")
    }
    missing = [
        key
        for key in ("username", "password")
        if not values[key] and (key == "password" or kind.username_required)
    ]
    if missing:
        raise DiscoverySecretError(
            f"{kind.op_id}: the Vault secret at secret_ref={secret_ref!r} (mount={mount!r}) "
            f"has no text under {' and '.join(repr(key) for key in missing)}. Nothing was sent."
        )
    return values["username"], values["password"]


def _holds_secret(text: str, secret: str) -> bool:
    return secret in text or json.dumps(secret)[1:-1] in text


def _error_without_secret(exc: httpx.HTTPStatusError, secret: str) -> httpx.HTTPStatusError:
    """A copy of *exc* with no request body and no text that holds *secret*.

    Keeps the status and the request headers (the dispatcher compares the
    sent token on a ``401``). The answer keeps only the vendor's error code,
    message and remediation, and none of them when the raw answer holds the
    password.
    """
    status = exc.response.status_code
    request = httpx.Request(exc.request.method, exc.request.url, headers=exc.request.headers)
    try:
        raw = exc.response.text
        body = exc.response.json()
    except (ValueError, UnicodeDecodeError, httpx.StreamError):
        raw, body = "", None
    kept: dict[str, str] = {}
    if isinstance(body, dict) and not _holds_secret(raw, secret):
        kept = {key: body[key] for key in _KEPT_ERROR_FIELDS if isinstance(body.get(key), str)}
    response = (
        httpx.Response(status, json=kept, request=request)
        if kept
        else httpx.Response(status, request=request)
    )
    return httpx.HTTPStatusError(
        f"VCF Installer answered HTTP {status} for {request.method} {request.url.path}",
        request=request,
        response=response,
    )


async def _discover(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
    kind: _Discovery,
) -> dict[str, Any]:
    """Run one discovery call: version check, Vault read, then the vendor POST."""
    if kind.minimum is not None:
        require_installer_version(
            target, op_id=kind.op_id, minimum=kind.minimum, feature=kind.feature
        )
    username, password = await _read_login(operator, target, params, kind)
    body: dict[str, Any] = {
        "address": params["address"],
        "sslThumbprint": params["ssl_thumbprint"],
        kind.password_field: password,
    }
    if username:
        body[kind.username_field] = username
    try:
        return await connector._post_json(target, kind.path, operator=operator, json=body)
    except httpx.HTTPStatusError as exc:
        error = _error_without_secret(exc, password)
    # Raised outside the except block, so the original error (its request
    # holds the password) is not chained to the new one.
    raise error


async def installer_discovery_vcenter_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.discovery.vcenter`` — vendor ``VcenterDiscoveryResult``."""
    return await _discover(connector, operator, target, params, _VCENTER)


async def installer_discovery_sddcm_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.discovery.sddc_manager`` — vendor ``SddcManagerDiscoveryResult`` (9.1+)."""
    return await _discover(connector, operator, target, params, _SDDC_MANAGER)


async def installer_discovery_vcfops_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.discovery.vcf_operations`` — vendor ``VcfOperationsDiscoveryResult``."""
    return await _discover(connector, operator, target, params, _VCF_OPERATIONS)


async def installer_sddc_cancel_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.cancel`` — ``DELETE /v1/tasks/{id}``.

    The vendor answers ``200`` with no body when it takes the cancel, so the
    handler answers ``{"id", "status": "cancel_requested"}``. A ``409`` (the
    task is not ``IN_PROGRESS``) or ``404`` propagates as
    :class:`httpx.HTTPStatusError`; the dispatcher puts the vendor message in
    the answer. The op waits for approval, so by the time this runs a person
    has approved it. The id must be one plain path segment (:func:`path_id`).
    """
    task_id = path_id(params["id"], op_id=INSTALLER_SDDC_CANCEL_OP_ID)
    await connector._post_json(
        target, _TASK_PATH.replace("{id}", task_id), operator=operator, verb="DELETE"
    )
    return {"id": task_id, "status": "cancel_requested"}


async def installer_appliance_info_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.appliance.info`` — the vendor ``ApplianceInfo`` as it is.

    9.0 sends ``version`` and ``role``; 9.1 adds the DNS, NTP and IP fields.
    """
    return await connector._get_json(target, _APPLIANCE_INFO_PATH, operator=operator)


def _release_row(release: dict[str, Any]) -> dict[str, Any]:
    row = {key: release[key] for key in RELEASE_ROW_KEYS if key in release}
    bom = release.get("bom")
    if isinstance(bom, list):
        row["bom"] = {
            str(item["name"]): item.get("version")
            for item in bom
            if isinstance(item, dict) and item.get("name")
        }
    return row


async def installer_release_list_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.release.list`` — ``GET /v1/releases``, one flat row per release.

    The optional ``version_eq`` / ``version_ge`` / ``include_only_compatible``
    params map to the vendor query filters; absent ones are not sent. Each
    vendor ``Release`` is cut to :data:`RELEASE_ROW_KEYS` plus a
    ``{component: version}`` ``bom`` map. Returns ``{"elements": [...],
    "pageMetadata"?}``.
    """
    query: dict[str, Any] = {}
    for param_key, vendor_key in _RELEASE_QUERY.items():
        value = params.get(param_key)
        if isinstance(value, bool):
            query[vendor_key] = "true" if value else "false"
        elif value:
            query[vendor_key] = str(value)
    payload = await connector._get_json(
        target, _RELEASES_PATH, operator=operator, params=query or None
    )
    elements = payload.get("elements")
    result: dict[str, Any] = {
        "elements": [
            _release_row(release)
            for release in (elements if isinstance(elements, list) else [])
            if isinstance(release, dict)
        ]
    }
    if "pageMetadata" in payload:
        result["pageMetadata"] = payload["pageMetadata"]
    return result


async def installer_sddc_spec_get_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.sddc.spec.get`` — ``GET /v1/sddcs/{id}/spec``, secrets replaced.

    Every field whose name marks a secret (``rootVcenterPassword``,
    ``credentials``, ``nsxtAdminPassword``, a licence key, ...) gets the
    ``[REDACTED:param_name]`` marker, at every depth, by the shared
    :func:`~meho_backplane.redaction.secret_fields.scrub_secret_fields` rule.
    The id must be one plain path segment (:func:`path_id`).
    """
    sddc_id = path_id(params["id"], op_id=INSTALLER_SDDC_SPEC_GET_OP_ID)
    payload = await connector._get_json(
        target, _SDDC_SPEC_PATH.replace("{id}", sddc_id), operator=operator
    )
    scrubbed: dict[str, Any] = scrub_secret_fields(payload)
    return scrubbed
