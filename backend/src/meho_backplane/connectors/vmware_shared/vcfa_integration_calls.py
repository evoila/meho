# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the integration ops of the two Automation connectors share.

Aria Automation 8 (connector ``vcfa-vra8-8.0``) and the tenant side of a
VCF Automation 9 "VM Apps" organization (connector ``vcfa-rest-9.0``) serve
the same integration, content source and IPAM provider APIs. The five
integration ops are written once (this module and the modules next to it)
and registered on both connectors.

Each connector has one method, ``_integration_send`` (see
:class:`IntegrationTransport`): it sends one request with the connector's
own login, logs in again once on a ``401``, and raises
:class:`httpx.HTTPStatusError` on any other error answer. :class:`Calls`
wraps it for the handlers.

This module holds the vendor paths, the checks of caller values that go into
a path, the read of a password or token from Vault, the vendor error text
(without any secret), the request tracker wait and :class:`Steps`, the list
of steps a multi-step change returns.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import quote

import httpx

from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "CONTENT_SOURCES_PATH",
    "CONTENT_SOURCE_PATH",
    "DEFAULT_WAIT_SECONDS",
    "EXTERNAL_IP_RANGES_PATH",
    "IAAS_API_VERSION",
    "INTEGRATIONS_PATH",
    "INTEGRATION_PATH",
    "IPAM_PACKAGE_IMPORT_PATH",
    "IPAM_PROVIDERS_PATH",
    "NETWORK_PROFILES_PATH",
    "REQUEST_TRACKER_PATH",
    "SYNC_HISTORY_PATH",
    "SYNC_REQUESTS_PATH",
    "SYNC_REQUEST_PATH",
    "Calls",
    "IaasNotServedError",
    "IntegrationTransport",
    "Steps",
    "content_rows",
    "follow_tracker",
    "hide_secrets",
    "now_iso",
    "path_with",
    "read_secret",
    "tracker_row",
    "vendor_text",
    "wait_for_tracker",
]

#: The IaaS API version MEHO asks for (the vendor's newest dated version).
IAAS_API_VERSION: Final[str] = "2021-07-15"

# Vendor paths. The reconcile lane checks each one, with its methods, against
# the vendor's API files (``tests/test_vcfa_integration_spec_reconcile.py``).
INTEGRATIONS_PATH: Final[str] = "/iaas/api/integrations"
INTEGRATION_PATH: Final[str] = "/iaas/api/integrations/{id}"
REQUEST_TRACKER_PATH: Final[str] = "/iaas/api/request-tracker/{id}"
EXTERNAL_IP_RANGES_PATH: Final[str] = "/iaas/api/external-network-ip-ranges"
NETWORK_PROFILES_PATH: Final[str] = "/iaas/api/network-profiles"
CONTENT_SOURCES_PATH: Final[str] = "/content/api/sources"
CONTENT_SOURCE_PATH: Final[str] = "/content/api/sources/{id}"
SYNC_REQUESTS_PATH: Final[str] = "/content/api/sourcecontrol/sync-requests"
SYNC_REQUEST_PATH: Final[str] = "/content/api/sourcecontrol/sync-requests/{id}"
SYNC_HISTORY_PATH: Final[str] = "/content/api/sourcecontrol/sync-history"
#: The IPAM provider calls. No published API file lists them; the vendor's
#: own web app uses them (the reconcile lane lists them as an exclusion).
IPAM_PROVIDERS_PATH: Final[str] = "/provisioning/ipam/api/providers"
IPAM_PACKAGE_IMPORT_PATH: Final[str] = "/provisioning/ipam/api/providers/packages/import"

#: How long to wait between two reads of a request tracker. Tests set it to 0.
POLL_SECONDS: float = 2.0

#: The states of a request tracker that end it.
TRACKER_END_STATES: Final[frozenset[str]] = frozenset({"FINISHED", "FAILED"})

#: A row list read asks for at most this many rows (``$top``).
LIST_TOP: Final[int] = 500

#: How long a change waits for a request tracker unless the call says otherwise.
DEFAULT_WAIT_SECONDS: Final[int] = 300

_HIDDEN_TEXT: Final[str] = "[MEHO hid this text: it held a password or token from Vault]"


class IntegrationTransport(Protocol):
    """The one method each connector adds for the integration ops."""

    async def _integration_send(
        self,
        target: Any,
        operator: Operator,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response: ...


class IaasNotServedError(RuntimeError):
    """The organization answers ``404`` on the IaaS integration list."""


@dataclass(frozen=True)
class Calls:
    """The connector, the caller and the target of one op call.

    ``ns`` is the op id prefix (``vcfa.tenant`` or ``vcfa.vra8``); the
    answers use it to name the next op.
    """

    connector: IntegrationTransport
    operator: Operator
    target: Any
    ns: str

    async def send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        return await self.connector._integration_send(
            self.target,
            self.operator,
            method,
            path,
            params=params,
            json=json,
            content=content,
            headers=headers,
        )

    async def json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        """Send one request and return its JSON answer (``{}`` when it has none)."""
        resp = await self.send(method, path, params=params, json=json)
        if resp.status_code == httpx.codes.NO_CONTENT or not resp.content:
            return {}
        return resp.json()

    async def iaas(self, method: str, path: str, *, json: Any = None, **query: Any) -> Any:
        """An IaaS call with the ``apiVersion`` query the vendor requires."""
        return await self.json(
            method, path, params={"apiVersion": IAAS_API_VERSION, **query}, json=json
        )

    def op(self, name: str) -> str:
        """The full id of the op *name* on this connector."""
        return f"{self.ns}.{name}"


def path_with(template: str, value: Any, what: str) -> str:
    """Put one caller value into *template*'s ``{...}`` part, checked and encoded.

    Refuses an empty value, ``.``, ``..`` and control characters, then
    percent-encodes it with no safe characters, so the value cannot reach
    another path.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or text in {".", ".."} or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError(f"{what} must be a plain id (not empty, '.' or '..'); nothing was sent")
    head, _, rest = template.partition("{")
    _, _, tail = rest.partition("}")
    return f"{head}{quote(text, safe='')}{tail}"


def content_rows(page: Any) -> list[dict[str, Any]]:
    """The rows of a vendor page (``{content: [...]}``) or of a bare list."""
    rows = page.get("content") if isinstance(page, Mapping) else page
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def now_iso() -> str:
    """The time now, ISO 8601 in UTC."""
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class _RefTarget:
    """Points the Vault read at the path a call names (not the target's login)."""

    name: str
    host: str
    secret_ref: str | None


async def read_secret(calls: Calls, params: Mapping[str, Any], prefix: str) -> str | None:
    """Read the secret a call names with ``<prefix>_secret_ref``, under the caller's identity.

    ``<prefix>_secret_mount`` (default ``secret``) and ``<prefix>_secret_key``
    (default ``password``) are optional. Returns ``None`` when the call names
    no ref. A Vault refusal raises ``SecretRefReadError`` (the dispatcher
    answers ``vault_credentials_read_error``). The value never enters a log,
    a result or an error.
    """
    param = f"{prefix}_secret_ref"
    secret_ref = str(params.get(param) or "").strip()
    if not secret_ref:
        return None
    mount = str(params.get(f"{prefix}_secret_mount") or DEFAULT_KV_MOUNT).strip()
    key = str(params.get(f"{prefix}_secret_key") or "password").strip()
    data = await load_param_secret_data(
        _RefTarget(
            name=str(getattr(calls.target, "name", "") or "unknown"),
            host=str(getattr(calls.target, "host", "") or "unknown"),
            secret_ref=secret_ref,
        ),
        calls.operator,
        param=param,
        mount=mount,
    )
    value = data.get(key) if isinstance(data, dict) else None
    secret = strip_credential_value(value) if isinstance(value, str) else ""
    if not secret:
        raise ValueError(
            f"the Vault secret named by {param} (mount {mount!r}) has no text under the key "
            f"{key!r}. Store it under that key, or set {prefix}_secret_key. Nothing was sent."
        )
    return secret


def hide_secrets(text: str, secrets: Iterable[str | None]) -> str:
    """*text*, or a fixed note when it holds one of *secrets* (as is or escaped)."""
    for secret in secrets:
        if not secret:
            continue
        forms = {secret, quote(secret, safe=""), secret.replace('"', '\\"')}
        if any(form in text for form in forms):
            return _HIDDEN_TEXT
    return text


def vendor_text(exc: httpx.HTTPStatusError, secrets: Iterable[str | None] = ()) -> str:
    """``HTTP <status>: <vendor message>``, from the answer only, without a secret.

    The request of *exc* (which may carry a password in its body) is not used.
    """
    resp = exc.response
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    parts: list[str] = []
    if isinstance(body, Mapping):
        for key in ("message", "errorMessage", "error", "details"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        errors = body.get("errors")
        if isinstance(errors, list):
            parts.extend(str(item) for item in errors[:5] if item)
    elif resp.text and not resp.headers.get("content-type", "").startswith("text/html"):
        parts.append(resp.text.strip()[:300])
    text = hide_secrets("; ".join(parts)[:600], secrets)
    return f"HTTP {resp.status_code}" + (f": {text}" if text else "")


def tracker_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The flat view of a vendor request tracker."""
    resources = row.get("resources")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "status": row.get("status"),
        "progress": row.get("progress"),
        "message": row.get("message"),
        "resources": [str(item) for item in resources] if isinstance(resources, list) else [],
    }


async def wait_for_tracker(
    calls: Calls, tracker: Mapping[str, Any], wait_seconds: float
) -> dict[str, Any]:
    """Read the request tracker until it ends or *wait_seconds* pass; return its flat view.

    A tracker that has not ended when the time is up comes back as it is
    (``status: INPROGRESS``); the caller reads it on with ``request_tracker_id``.
    """
    row = tracker_row(tracker)
    tracker_id = row["id"]
    if not tracker_id:
        raise RuntimeError("the vendor answered without a request tracker id")
    deadline = time.monotonic() + max(wait_seconds, 0)
    while row["status"] not in TRACKER_END_STATES and time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        read = await calls.iaas("GET", path_with(REQUEST_TRACKER_PATH, tracker_id, "tracker id"))
        row = tracker_row(read if isinstance(read, Mapping) else {})
        row["id"] = row["id"] or tracker_id
    return row


async def follow_tracker(
    calls: Calls, steps: Steps, step: str, sent: Any, wait: float, secrets: tuple[str | None, ...]
) -> dict[str, Any]:
    """Wait for the tracker a sent *step* answered with; mark the step; return the tracker."""
    tracker = await wait_for_tracker(calls, sent if isinstance(sent, Mapping) else {}, wait)
    if tracker["message"]:
        tracker["message"] = hide_secrets(str(tracker["message"]), secrets)
    if tracker["status"] == "FINISHED":
        steps.done(step)
    elif tracker["status"] == "FAILED":
        steps.failed(step, tracker["message"] or "the vendor's request tracker ended FAILED")
    else:
        steps.unknown(step, f"still running after {int(wait)} seconds")
    return tracker


class Steps:
    """The steps of a multi-step change: ``done``, ``failed``, ``unknown`` or ``not_run``."""

    def __init__(self, *names: str) -> None:
        self._rows: list[dict[str, Any]] = [
            {"step": name, "result": "not_run", "error": None} for name in names
        ]

    def done(self, name: str) -> None:
        self._set(name, "done", None)

    def failed(self, name: str, error: str) -> None:
        self._set(name, "failed", error)

    def unknown(self, name: str, error: str) -> None:
        """The step was sent, but MEHO does not know whether it worked."""
        self._set(name, "unknown", error)

    def _set(self, name: str, result: str, error: str | None) -> None:
        for row in self._rows:
            if row["step"] == name:
                row["result"], row["error"] = result, error

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]
