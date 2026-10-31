# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``srm.appliance.register`` -- register an SRM appliance with its vCenter and SSO.

The steps, in order:

1. ``running_task`` -- look for a running registration task
   (``GET /tasks``). If one runs, return its id and start nothing.
2. ``probe_certificate`` -- ``probe-ssl-connection`` on
   ``https://<vcenter>:443``. MEHO computes the SHA-256 fingerprint of the
   certificate the appliance saw and stops when it differs from the
   fingerprint in the input. The fingerprint is sent as the vendor writes
   it: SHA-256, in capitals, with colons.
3. ``validate_connection`` -- the SSO admin user (from Vault) and the
   vCenter address, checked by the appliance.
4. ``check_existing_registrations`` -- the appliance or the extension key
   is already registered: stop, and list what is there.
5. ``list_vc_services`` -- only up to 9.0: the vCenter id, sent with the
   registration together with the vCenter's fingerprint. 9.1 finds the
   vCenter from the SSO address itself.
6. ``register`` -- ``PUT /appliance/configuration-manager/configuration``.
   The vendor answers with a task; the answer says ``accepted`` and names
   the status read ``srm.appliance.task.get``. It never says done.

The vCenter address always goes out as ``https://<name>:443``: without the
scheme and the port the vendor uses ``http`` and port 80, and the 9.1
setup API answers ``500`` on the connection test.

A failed step returns the steps that ran, the failed step with the
vendor's error and the steps that did not run. Nothing is undone.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from meho_backplane.connectors.srm.appliance import setup_api_for
from meho_backplane.connectors.srm.setup_api import (
    SetupApi,
    SetupApiError,
    Steps,
    find_running_task,
    parse_ref,
    task_view,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.connectors.vmware_shared.dr_rest import DrTargetLike

__all__ = [
    "REGISTER_TASK_WORDS",
    "appliance_register",
    "normalise_sha256_fingerprint",
    "vcenter_uri",
]

DEFAULT_EXTENSION_KEY: Final = "com.vmware.vcDr"
#: Words in a task's type or description that mark a registration task.
REGISTER_TASK_WORDS: Final[tuple[str, ...]] = ("configur", "regist")
STATUS_READ_OP: Final = "srm.appliance.task.get"

_STEPS: Final[tuple[str, ...]] = (
    "running_task",
    "probe_certificate",
    "validate_connection",
    "check_existing_registrations",
    "list_vc_services",
    "register",
)
_HOST_NAME: Final = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_HEX64: Final = re.compile(r"^[0-9A-F]{64}$")


def vcenter_uri(raw: Any) -> str:
    """Return ``https://<name>:443`` for a vCenter name; accept that form as input too."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("vcenter must be the vCenter's host name")
    text = raw.strip()
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme != "https" or parts.path not in ("", "/") or parts.query:
            raise ValueError("vcenter must be a host name, or https://<name>:443")
        if parts.port not in (None, 443) or parts.username or not parts.hostname:
            raise ValueError("vcenter must use port 443")
        text = parts.hostname
    elif text.endswith(":443"):
        text = text[: -len(":443")]
    if not _HOST_NAME.match(text):
        raise ValueError("vcenter must be a host name, for example <vcenter-fqdn>")
    return f"https://{text.lower()}:443"


def normalise_sha256_fingerprint(raw: Any) -> str:
    """Return a SHA-256 fingerprint as ``AA:BB:...`` (capitals, colons); else raise."""
    if not isinstance(raw, str):
        raise ValueError("the fingerprint must be a string")
    flat = raw.strip().replace(":", "").upper()
    if not _HEX64.match(flat):
        raise ValueError(
            "the fingerprint must be a SHA-256 fingerprint: 64 hex digits, with or without colons"
        )
    return ":".join(flat[i : i + 2] for i in range(0, 64, 2))


def _fingerprint_of(probe: Any) -> str:
    """SHA-256 fingerprint of the certificate in a ``probe-ssl-connection`` answer."""
    data = probe if isinstance(probe, dict) else {}
    pem = data.get("certificate")
    if isinstance(pem, str) and "-----BEGIN CERTIFICATE-----" in pem:
        body = pem.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END CERTIFICATE-----")[0]
        digest = hashlib.sha256(base64.b64decode("".join(body.split()))).hexdigest()
        return normalise_sha256_fingerprint(digest)
    try:
        return normalise_sha256_fingerprint(data.get("thumbprint"))
    except ValueError:
        raise SetupApiError(
            "the appliance's certificate probe returned no SHA-256 fingerprint"
        ) from None


def _version_tuple(raw: Any) -> tuple[int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)", str(raw or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


async def _probe(api: SetupApi, uri: str) -> str:
    probe = await api.call(
        "POST", "/appliance/certificates/server/actions/probe-ssl-connection", json={"uri": uri}
    )
    return _fingerprint_of(probe)


def _registration_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("registration_type", "extension_key", "service_name", "server_url", "version")
    return {key: row.get(key) for key in keys}


async def _vc_service(api: SetupApi, connection: dict[str, Any], uri: str) -> dict[str, str]:
    """Up to 9.0: the vCenter id from ``list-vc-services`` and the vCenter's fingerprint."""
    answer = await api.call(
        "POST", "/appliance/configuration-manager/actions/list-vc-services", json=connection
    )
    rows = [row for row in (answer or {}).get("list") or [] if isinstance(row, dict)]
    host = urlsplit(uri).hostname
    match = [row for row in rows if urlsplit(str(row.get("url") or "")).hostname == host]
    if not match and len(rows) == 1:
        match = rows
    if len(match) != 1 or not match[0].get("service_id"):
        found = ", ".join(str(row.get("url")) for row in rows) or "none"
        raise SetupApiError(f"list-vc-services found no single vCenter for {host} (found: {found})")
    vc_url = str(match[0].get("url") or "")
    vc_host = urlsplit(vc_url).hostname
    thumbprint = (
        connection["psc_thumbprint"]
        if vc_host in (None, host)
        else await _probe(api, f"https://{vc_host}:443")
    )
    return {"vc_instance_id": str(match[0]["service_id"]), "vc_thumbprint": thumbprint}


class _StoppedError(Exception):
    """A check stopped the registration before the change; the answer is ready."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__("stopped")
        self.answer = answer


async def _checks(
    api: SetupApi,
    steps: Steps,
    params: dict[str, Any],
    sso: dict[str, str],
) -> dict[str, Any]:
    """Steps 2 to 5. Return the ``connection`` block of the registration."""
    uri = vcenter_uri(params.get("vcenter"))
    expected = normalise_sha256_fingerprint(params.get("vcenter_fingerprint"))
    steps.start("probe_certificate")
    seen = await _probe(api, uri)
    if seen != expected:
        steps.failed(
            "probe_certificate",
            f"the vCenter certificate has fingerprint {seen}, not the expected {expected}",
        )
        raise _StoppedError({"outcome": "refused", "seen_fingerprint": seen})
    steps.done("probe_certificate", fingerprint=seen)
    credentials = {"admin_user": sso["username"], "admin_password": sso["password"]}
    connection = {"psc_uri": uri, "psc_thumbprint": seen}
    vc_spec = {"connection_spec": connection, "credentials": credentials}
    steps.start("validate_connection")
    await api.call(
        "POST", "/appliance/configuration-manager/actions/validate-connection", json=vc_spec
    )
    steps.done("validate_connection")
    steps.start("check_existing_registrations")
    existing = await api.call(
        "POST",
        "/appliance/configuration-manager/actions/check-existing-registrations",
        json={"extension_key": params["extension_key"], "connection_spec": vc_spec},
    )
    rows = [row for row in (existing or {}).get("list") or [] if isinstance(row, dict)]
    if rows:
        steps.failed(
            "check_existing_registrations",
            f"{len(rows)} registration(s) for this appliance or extension key already exist",
        )
        raise _StoppedError(
            {"outcome": "refused", "existing_registrations": [_registration_row(r) for r in rows]}
        )
    steps.done("check_existing_registrations")
    return connection


def _register_body(
    params: dict[str, Any], connection: dict[str, Any], sso: dict[str, str]
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "site_name": params["site_name"],
        "admin_email": params["admin_email"],
        "extension_key": params["extension_key"],
        "connection": connection,
        "credentials": {"admin_user": sso["username"], "admin_password": sso["password"]},
    }
    return body


async def appliance_register(
    connector: SrmConnector, operator: Operator, target: DrTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``srm.appliance.register``; see the module docstring for the steps."""
    params = {"extension_key": DEFAULT_EXTENSION_KEY, **params}
    for name in ("site_name", "admin_email"):
        if not isinstance(params.get(name), str) or not params[name].strip():
            raise ValueError(f"{name} is required")
    vcenter_uri(params.get("vcenter"))
    normalise_sha256_fingerprint(params.get("vcenter_fingerprint"))
    sso_ref = parse_ref(params, "sso_secret_ref")
    api = setup_api_for(connector, operator, target, params)
    steps = Steps(_STEPS)
    answer: dict[str, Any] = {"outcome": "failed", "task_id": None, "task": None}

    async def load_sso() -> dict[str, str]:
        return await connector.setup_credentials_loader(target, operator, "sso_secret_ref", sso_ref)

    try:
        await _run(api, steps, params, load_sso, answer)
    except _StoppedError as stop:
        answer.update(stop.answer)
    except (SetupApiError, ValueError) as exc:
        steps.fail(str(exc))
    answer["steps"] = steps.as_list()
    answer["status_read"] = (
        {"op_id": STATUS_READ_OP, "params": {"task_id": answer["task_id"]}}
        if answer["task_id"]
        else None
    )
    return answer


async def _run(
    api: SetupApi,
    steps: Steps,
    params: dict[str, Any],
    load_sso: Callable[[], Awaitable[dict[str, str]]],
    answer: dict[str, Any],
) -> None:
    steps.start("running_task")
    version = _version_tuple((await api.call("GET", "/appliance")).get("appliance_version"))
    if version is None:
        raise SetupApiError("the appliance did not say its version")
    running = await find_running_task(api, REGISTER_TASK_WORDS)
    if running is not None:
        steps.done("running_task", found_task_id=running.get("id"))
        answer.update(outcome="already_running", task_id=running.get("id"), task=task_view(running))
        return
    steps.done("running_task")
    steps.start("probe_certificate")
    sso = await load_sso()
    connection = await _checks(api, steps, params, sso)
    if version < (9, 1):
        steps.start("list_vc_services")
        connection = {**connection, **await _vc_service(api, connection, connection["psc_uri"])}
        steps.done("list_vc_services")
    else:
        steps.skip("list_vc_services")
    steps.start("register")
    task = await api.call(
        "PUT",
        "/appliance/configuration-manager/configuration",
        json=_register_body(params, connection, sso),
    )
    steps.done("register")
    view = task_view(task)
    answer.update(outcome="accepted", task_id=view["task_id"], task=view)
