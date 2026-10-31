# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Installer settings: the proxy, CEIP, system settings and removing a trusted certificate.

The bodies of five typed operations; the metadata lives in
:mod:`.typed_ops_system`.

* ``installer.system.proxy.get`` — ``GET /v1/system/proxy-configuration``,
  password removed. With ``task_id`` it is also the status read of a proxy
  change (``GET /v1/tasks/{id}``).
* ``installer.system.proxy.set`` — ``PATCH /v1/system/proxy-configuration``.
  The vendor answers ``202`` with a job; the answer says ``accepted`` with the
  job id. The password comes from Vault.
* ``installer.system.settings.get`` — ``GET /v1/system/ceip`` and
  ``GET /v1/system``. With ``task_id`` it is also the status read of a CEIP
  change.
* ``installer.system.settings.set`` — up to two steps: ``PATCH
  /v1/system/ceip`` (a job; on 9.0 the body is a plain JSON string, from 9.1
  on an object with ``status``), then ``PATCH /v1/system`` (done at once).
  A failure part-way returns each step: done, accepted, failed or not run.
  Nothing is undone.
* ``installer.system.trusted-certificates.remove`` — ``DELETE
  /v1/sddc-manager/trusted-certificates/{alias}``. The answer warns when the
  certificate belongs to the depot that is set.

Every write goes through the connector's session-retry helper: a ``401``
means the request was refused before the appliance did anything, so one new
login and one new attempt cannot apply a change twice. A vendor refusal of a
change comes back as ``status: rejected`` with the vendor error code and
text, the same way ``installer.system.depot.set`` answers ``depot_error``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote

import httpx
import structlog
from cryptography import x509
from cryptography.x509.oid import NameOID

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vcf_installer._secret_refs import hide_secret, read_secret_ref
from meho_backplane.connectors.vcf_installer._vendor_tasks import (
    find_running_task,
    is_not_reachable,
    read_task,
)
from meho_backplane.connectors.vcf_installer._version_gate import installer_version
from meho_backplane.connectors.vcf_installer.typed_reads import scrub_password_keys

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_installer.connector import InstallerConnector
    from meho_backplane.connectors.vcf_installer.session import InstallerTargetLike

__all__ = [
    "INSTALLER_PROXY_GET_OP_ID",
    "INSTALLER_PROXY_SET_OP_ID",
    "INSTALLER_SETTINGS_GET_OP_ID",
    "INSTALLER_SETTINGS_SET_OP_ID",
    "INSTALLER_TRUSTED_CERTIFICATE_REMOVE_OP_ID",
    "SYSTEM_DECLARED_OP_IDS",
    "installer_proxy_get_impl",
    "installer_proxy_set_impl",
    "installer_settings_get_impl",
    "installer_settings_set_impl",
    "installer_trusted_certificate_remove_impl",
]

_log = structlog.get_logger(__name__)

INSTALLER_PROXY_GET_OP_ID: Final = "installer.system.proxy.get"
INSTALLER_PROXY_SET_OP_ID: Final = "installer.system.proxy.set"
INSTALLER_SETTINGS_GET_OP_ID: Final = "installer.system.settings.get"
INSTALLER_SETTINGS_SET_OP_ID: Final = "installer.system.settings.set"
INSTALLER_TRUSTED_CERTIFICATE_REMOVE_OP_ID: Final = "installer.system.trusted-certificates.remove"

# --- Hand-coded wire paths (checked against the pinned 9.0 and 9.1 files) ---
_PROXY_PATH: Final = "/v1/system/proxy-configuration"
_CEIP_PATH: Final = "/v1/system/ceip"
_SYSTEM_PATH: Final = "/v1/system"
_TRUSTED_CERTIFICATE_PATH: Final = "/v1/sddc-manager/trusted-certificates/{alias}"
_TRUSTED_CERTIFICATES_PATH: Final = "/v1/sddc-manager/trusted-certificates"
_DEPOT_SETTINGS_PATH: Final = "/v1/system/settings/depot"

#: Every ``METHOD:/path`` this module calls. The reconcile lane checks each one
#: against both pinned installer files (9.0 and 9.1).
SYSTEM_DECLARED_OP_IDS: frozenset[str] = frozenset(
    {
        f"GET:{_PROXY_PATH}",
        f"PATCH:{_PROXY_PATH}",
        f"GET:{_CEIP_PATH}",
        f"PATCH:{_CEIP_PATH}",
        f"GET:{_SYSTEM_PATH}",
        f"PATCH:{_SYSTEM_PATH}",
        f"DELETE:{_TRUSTED_CERTIFICATE_PATH}",
        f"GET:{_TRUSTED_CERTIFICATES_PATH}",
        f"GET:{_DEPOT_SETTINGS_PATH}",
        "GET:/v1/tasks",
        "GET:/v1/tasks/{id}",
    }
)

#: CEIP states that mean a change is still running.
_CEIP_CHANGING: Final = frozenset({"ENABLING", "DISABLING"})


def _vendor_error(exc: httpx.HTTPStatusError, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """``{http_status, error_code, message}`` of a vendor refusal, secrets hidden."""
    try:
        body = exc.response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    message = body.get("message") or exc.response.reason_phrase
    return {
        "http_status": exc.response.status_code,
        "error_code": body.get("errorCode"),
        "message": hide_secret(message, secrets),
    }


def _not_reachable_error(exc: httpx.TransportError) -> str:
    return f"no answer from the appliance ({type(exc).__name__}); the change may or may not be done"


async def _status_block(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    task_id = params.get("task_id")
    if not task_id:
        return None
    return await read_task(connector, operator, target, str(task_id))


# --------------------------------------------------------------------- proxy


async def installer_proxy_get_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.proxy.get`` — the proxy settings, and the job state with ``task_id``.

    The ``password`` key is removed at every depth. With ``task_id``, the
    job is read first; when the appliance does not answer, the answer is
    ``{"proxy": None, "task": {"outcome": "not_reachable", ...}}``.
    """
    task = await _status_block(connector, operator, target, params)
    result: dict[str, Any] = {}
    if task is not None:
        result["task"] = task
        if task["outcome"] == "not_reachable":
            result["proxy"] = None
            return result
    payload = await connector._get_json_with_session_retry(target, _PROXY_PATH, operator=operator)
    result["proxy"] = scrub_password_keys(payload)
    return result


async def _proxy_body(
    operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The vendor ``ProxyConfiguration`` body and the secrets it holds."""
    if not params["enabled"]:
        return {"isEnabled": False}, ()
    body: dict[str, Any] = {
        "isEnabled": True,
        "host": params["host"],
        "port": params["port"],
        "transferProtocol": params.get("transfer_protocol") or "HTTP",
        "isAuthenticated": False,
    }
    username = params.get("username")
    if not username:
        return body, ()
    ref = {
        "secret_ref": params.get("password_secret_ref"),
        "secret_mount": params.get("password_secret_mount"),
        "secret_key": params.get("password_secret_key"),
    }
    password = await read_secret_ref(operator, target, ref, param="password_secret_ref")
    body.update(isAuthenticated=True, username=username, password=password)
    return body, (password,)


async def installer_proxy_set_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.proxy.set`` — ``PATCH /v1/system/proxy-configuration``.

    First looks for a proxy job that has not ended; if one runs, returns
    ``already_running`` with its id and starts nothing. Else sends the body
    and returns ``accepted`` with the vendor job id as soon as the appliance
    takes the job. The status read is ``installer.system.proxy.get`` with
    ``task_id``. A vendor refusal returns ``rejected`` with the vendor text
    (the password hidden). A lost reply returns ``unknown``.
    """
    status_read = {"op_id": INSTALLER_PROXY_GET_OP_ID}
    running = await find_running_task(connector, operator, target, word="PROXY", exclude={"CLOUD"})
    if running is not None:
        return {
            "status": "already_running",
            "task_id": running.get("id"),
            "vendor_status": running.get("status"),
            "status_read": {**status_read, "params": {"task_id": running.get("id")}},
        }
    body, secrets = await _proxy_body(operator, target, params)
    try:
        task = await connector._post_json_with_session_retry(
            target, _PROXY_PATH, operator=operator, verb="PATCH", json=body
        )
    except httpx.HTTPStatusError as exc:
        return {"status": "rejected", **_vendor_error(exc, secrets)}
    except httpx.TransportError as exc:
        if not is_not_reachable(exc):
            raise
        return {
            "status": "unknown",
            "message": _not_reachable_error(exc),
            "status_read": status_read,
        }
    task_id = task.get("id")
    return {
        "status": "accepted",
        "task_id": task_id,
        "vendor_status": task.get("status"),
        "status_read": {**status_read, "params": {"task_id": task_id}},
        "proxy": scrub_password_keys(body),
    }


# ----------------------------------------------------------- CEIP and system


async def installer_settings_get_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.settings.get`` — CEIP state and system settings, plus the job state.

    Same ``task_id`` rule as :func:`installer_proxy_get_impl`.
    """
    task = await _status_block(connector, operator, target, params)
    result: dict[str, Any] = {}
    if task is not None:
        result["task"] = task
        if task["outcome"] == "not_reachable":
            result.update(ceip=None, system=None)
            return result
    result["ceip"] = await connector._get_json_with_session_retry(
        target, _CEIP_PATH, operator=operator
    )
    result["system"] = await connector._get_json_with_session_retry(
        target, _SYSTEM_PATH, operator=operator
    )
    return result


def _ceip_body(target: InstallerTargetLike, enabled: bool) -> Any:
    """The CEIP body for the target's version.

    9.0 takes a plain JSON string (``"ENABLED"`` / ``"DISABLED"``); 9.1 and
    newer take ``{"status": "ENABLE" | "DISABLE"}``. An unknown version gets
    the 9.1 shape, the connector's own version.
    """
    version = installer_version(target)
    if version is not None and version.release[:2] == (9, 0):
        return "ENABLED" if enabled else "DISABLED"
    return {"status": "ENABLE" if enabled else "DISABLE"}


def _status_read(status_read: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    """The status read, with ``task_id`` in its params when there is one."""
    return {**status_read, "params": {"task_id": task_id}} if task_id else status_read


class _Steps:
    """The steps of a settings change: done, accepted, failed, unknown or not_run."""

    def __init__(self, names: list[str]) -> None:
        self.rows: list[dict[str, Any]] = [{"step": name, "result": "not_run"} for name in names]

    def set(self, name: str, result: str, **extra: Any) -> None:
        for row in self.rows:
            if row["step"] == name:
                row.update(result=result, **extra)

    def status(self) -> str:
        results = [row["result"] for row in self.rows]
        if any(result in ("failed", "unknown") for result in results):
            ran = any(result in ("done", "accepted") for result in results)
            return "partial" if ran else "failed"
        return "accepted" if "accepted" in results else "done"


async def _ceip_running(
    connector: InstallerConnector, operator: Operator, target: InstallerTargetLike
) -> dict[str, Any] | None:
    """A CEIP change that has not ended, or ``None``."""
    running = await find_running_task(connector, operator, target, word="CEIP")
    if running is not None:
        return {"task_id": running.get("id"), "vendor_status": running.get("status")}
    ceip = await connector._get_json_with_session_retry(target, _CEIP_PATH, operator=operator)
    state = str(ceip.get("status") or "").upper()
    if state in _CEIP_CHANGING:
        return {"task_id": None, "vendor_status": state}
    return None


async def _run_step(steps: _Steps, name: str, call: Any) -> dict[str, Any] | None:
    """Run one write; record the result. ``None`` when the step did not succeed."""
    try:
        return cast("dict[str, Any]", await call)
    except httpx.HTTPStatusError as exc:
        steps.set(name, "failed", error=_vendor_error(exc))
    except httpx.TransportError as exc:
        if not is_not_reachable(exc):
            raise
        steps.set(name, "unknown", error={"message": _not_reachable_error(exc)})
    return None


async def installer_settings_set_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.settings.set`` — CEIP on or off, then the system setting.

    A CEIP change that has not ended means ``already_running``: nothing is
    started. Each step that fails stops the rest; the steps that ran are not
    undone.
    """
    status_read = {"op_id": INSTALLER_SETTINGS_GET_OP_ID}
    names = [
        name
        for name, key in (("ceip", "ceip_enabled"), ("system", "max_domains_in_subscription"))
        if params.get(key) is not None
    ]
    steps = _Steps(names)
    task_id: str | None = None
    if "ceip" in names:
        running = await _ceip_running(connector, operator, target)
        if running is not None:
            return {
                "status": "already_running",
                **running,
                "steps": steps.rows,
                "status_read": _status_read(status_read, running["task_id"]),
            }
        task = await _run_step(
            steps,
            "ceip",
            connector._post_json_with_session_retry(
                target,
                _CEIP_PATH,
                operator=operator,
                verb="PATCH",
                # 9.0 takes a bare JSON string; httpx serialises any JSON value.
                json=cast("dict[str, Any]", _ceip_body(target, bool(params["ceip_enabled"]))),
            ),
        )
        if task is None:
            return {"status": steps.status(), "steps": steps.rows, "status_read": status_read}
        task_id = task.get("id")
        steps.set("ceip", "accepted", task_id=task_id, vendor_status=task.get("status"))
    if "system" in names:
        body = {"maxAllowedDomainsInSubscription": params["max_domains_in_subscription"]}
        done = await _run_step(
            steps,
            "system",
            connector._post_json_with_session_retry(
                target, _SYSTEM_PATH, operator=operator, verb="PATCH", json=body
            ),
        )
        if done is not None:
            steps.set("system", "done")
    result: dict[str, Any] = {"status": steps.status(), "steps": steps.rows}
    if task_id is not None:
        result["task_id"] = task_id
    result["status_read"] = _status_read(status_read, task_id)
    return result


# ------------------------------------------------- trusted certificate remove


def _names_in_certificate(pem: Any) -> set[str]:
    """The lower-case DNS names and common names of a PEM certificate."""
    if not isinstance(pem, str):
        return set()
    try:
        cert = x509.load_pem_x509_certificate(pem.encode())
    except ValueError:
        return set()
    names = {
        str(attr.value).lower() for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    }
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return names
    names.update(name.lower() for name in san.value.get_values_for_type(x509.DNSName))
    names.update(str(ip) for ip in san.value.get_values_for_type(x509.IPAddress))
    return names


async def _depot_host(
    connector: InstallerConnector, operator: Operator, target: InstallerTargetLike
) -> str | None:
    """The host name of the depot that is set, or ``None`` (none set, or not readable)."""
    try:
        settings = await connector._get_json_with_session_retry(
            target, _DEPOT_SETTINGS_PATH, operator=operator
        )
    except httpx.HTTPStatusError:
        return None
    config = settings.get("depotConfiguration")
    host = config.get("hostname") if isinstance(config, dict) else None
    return host.lower() if isinstance(host, str) and host else None


async def _certificate_pem(
    connector: InstallerConnector, operator: Operator, target: InstallerTargetLike, alias: str
) -> str | None:
    try:
        page = await connector._get_json_with_session_retry(
            target, _TRUSTED_CERTIFICATES_PATH, operator=operator
        )
    except httpx.HTTPStatusError:
        return None
    for row in page.get("elements") or []:
        if isinstance(row, dict) and row.get("alias") == alias:
            pem = row.get("certificate")
            return pem if isinstance(pem, str) else None
    return None


async def installer_trusted_certificate_remove_impl(
    connector: InstallerConnector,
    operator: Operator,
    target: InstallerTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``installer.system.trusted-certificates.remove`` — remove one certificate by alias.

    Before the delete, reads the depot host and the certificate, so the
    answer can warn when the certificate names the depot that is set. An
    unknown alias (vendor ``404``) raises
    :class:`~meho_backplane.connectors.base.ConnectorResourceNotFoundError`.
    """
    alias = str(params["alias"])
    depot_host = await _depot_host(connector, operator, target)
    pem = await _certificate_pem(connector, operator, target, alias)
    try:
        await connector._post_json_with_session_retry(
            target,
            _TRUSTED_CERTIFICATE_PATH.replace("{alias}", quote(alias, safe="")),
            operator=operator,
            verb="DELETE",
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.NOT_FOUND:
            raise ConnectorResourceNotFoundError(
                [alias],
                f"no trusted certificate with alias {alias!r} on the installer; "
                "list them with installer.system.trusted-certificates.list",
            ) from exc
        raise
    result: dict[str, Any] = {"status": "removed", "alias": alias}
    if depot_host and (depot_host in _names_in_certificate(pem) or depot_host in alias.lower()):
        result["warning"] = (
            f"This certificate belongs to the depot that is set ({depot_host}). The installer "
            "can no longer reach that depot over HTTPS until the certificate is added again "
            "with installer.system.trusted-certificates.add."
        )
    _log.info("installer_trusted_certificate_removed", target=target.name, alias=alias)
    return result
