# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The HCX Manager appliance management API (port 9443), as the setup operations use it.

The setup of an HCX Manager (vCenter, single sign-on, NSX, location, role
mapping, activation and the restart of the application service) is not in
the main HCX API (``/hybridity/api`` on port 443). It is in the appliance
management API on port 9443 of the same host: ``/api/admin/global/config/...``
and ``/components/appengine``.

Login: HTTP basic with the appliance account of the HCX Manager (the
``admin`` user set at deployment), on every call. There is no session, so a
login cannot expire between two calls. The account comes from the target:

* ``extras.appliance_secret_ref`` -- a Vault reference to a secret with
  ``username`` and ``password``, when the target sets it;
* else the target's own secret (``secret_ref``), read with the connector's
  credential loader. On a new HCX Manager the appliance account is the only
  account, so the target secret usually holds it.

Each operation opens one :class:`ApplianceApi` (:func:`appliance_session`):
one credential read and one short-lived HTTP client with the target's TLS
settings and the same address check as the pooled client. The client is
closed when the operation ends.

Errors: a vendor answer with an error status raises
:class:`HcxApplianceError` (a
:class:`~meho_backplane.connectors.base.ConnectorUpstreamError`), so the
dispatcher shows ``http_status`` and the vendor's message. Before that, every
secret the call sent (a password, its base64 form, a licence key) is cut out
of the vendor's text. A message that names an untrusted certificate or an
address HCX cannot reach gets a hint in ``remediation``.

Traces: the flight recorder gets the request body with every secret value
replaced, and the answer masked by
:meth:`~meho_backplane.connectors.hcx.connector.HcxConnector.trace_response_body`.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors.adapters.http import (
    _read_capped_json_response,
    json_payload_or_empty,
)
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.hcx.session import HcxTargetLike
from meho_backplane.connectors.hcx.typed_reads import REDACTED, redact_secrets, rows_of
from meho_backplane.flight_recorder import capture as flight_recorder_capture

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = [
    "APPLIANCE_PORT",
    "APPLIANCE_SECRET_EXTRA",
    "ApplianceApi",
    "HcxApplianceError",
    "HcxManagerSetupError",
    "RefTarget",
    "appliance_session",
    "base64_text",
    "scrub",
    "vendor_message",
]

#: Port of the appliance management API.
APPLIANCE_PORT: Final = 9443

#: Target ``extras`` key with the Vault reference of the appliance account.
APPLIANCE_SECRET_EXTRA: Final = "appliance_secret_ref"

#: Longest vendor text kept in an error.
_VENDOR_TEXT_MAX: Final = 1000

#: Body fields whose value is a secret the setup sends.
_SENT_SECRET_FIELDS: Final[frozenset[str]] = frozenset({"password", "activationKey"})

_CERT_WORDS: Final = ("certificate", "pkix", "ssl", "tls", "trust")
_REACH_WORDS: Final = (
    "unable to connect",
    "could not connect",
    "connection refused",
    "unreachable",
    "timed out",
    "timeout",
    "unknownhost",
    "unknown host",
    "no route",
)
_CERT_HINT: Final = (
    "HCX does not trust the certificate of that address. Trust it first with "
    "hcx.certificate.trust, then run this operation again."
)
_REACH_HINT: Final = (
    "HCX could not reach that address. Check the URL, the DNS name and the network "
    "path from the HCX Manager, then run this operation again."
)
_LOGIN_HINT: Final = (
    "The HCX Manager refused the appliance account. Check the secret named by the "
    "target's extras.appliance_secret_ref, or the target's own secret when that is not "
    "set: it must hold the username and password of the HCX appliance account."
)


class HcxManagerSetupError(RuntimeError):
    """MEHO refused a setup call before or between vendor calls; ``remediation`` says what next."""

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.remediation = remediation


class HcxApplianceError(ConnectorUpstreamError):
    """The appliance API answered with an error status. The vendor text has no sent secret."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int,
        upstream_message: str | None,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status, upstream_message=upstream_message)
        self.remediation = remediation


class _ApplianceTarget:
    """The target, seen on the appliance port. Every other field is the target's own."""

    port = APPLIANCE_PORT

    def __init__(self, target: HcxTargetLike) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


@dataclass(slots=True)
class RefTarget:
    """Points the shared Vault read at a reference other than the target's own."""

    name: str
    host: str
    secret_ref: str | None


def base64_text(value: str) -> str:
    """*value* as base64 text: HCX takes the vCenter and NSX passwords in this form."""
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Return *text* with each secret, and its base64 form, replaced by ``***REDACTED***``."""
    for secret in secrets:
        if not secret:
            continue
        for form in (secret, base64_text(secret)):
            text = text.replace(form, REDACTED)
    return text


def _mask_sent_secrets(value: Any) -> Any:
    """A copy of *value* with every non-empty sent secret field (password, key) replaced."""
    if isinstance(value, dict):
        return {
            key: REDACTED if key in _SENT_SECRET_FIELDS and item else _mask_sent_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_sent_secrets(item) for item in value]
    return value


def _messages_of(body: Any) -> list[str]:
    """The ``message`` texts of a vendor error body: top level, ``errors`` and row errors."""
    if not isinstance(body, dict):
        return []
    messages: list[str] = []
    for holder in [body, *rows_of(body)]:
        for error in holder.get("errors") or []:
            if isinstance(error, dict) and error.get("message"):
                messages.append(str(error["message"]))
            elif isinstance(error, str):
                messages.append(error)
    if not messages and isinstance(body.get("message"), str):
        messages.append(body["message"])
    return messages


def _refusal_text(body: Any) -> str | None:
    """The refusal text of a ``2xx`` answer that still says no, else ``None``.

    The role mapping call answers ``{"isSuccess": false, "message": ...}``;
    the config calls can answer ``{"errors": [...]}`` without any row.
    """
    if not isinstance(body, dict):
        return None
    if body.get("isSuccess") is False:
        return str(body.get("message") or "the HCX Manager answered isSuccess: false")
    if body.get("errors") and not rows_of(body):
        return "; ".join(_messages_of(body)) or "the HCX Manager answered with errors"
    return None


def vendor_message(response: httpx.Response, secrets: Iterable[str] = ()) -> str:
    """The vendor's error text of *response*, without the sent secrets, at most 1000 characters."""
    try:
        body = response.json()
    except ValueError:
        body = None
    text = "; ".join(_messages_of(body)) or response.text
    return scrub(text, secrets)[:_VENDOR_TEXT_MAX]


def _hint(status: int, text: str) -> str | None:
    """The next step for a refused call: a login hint, a certificate hint or a reach hint."""
    if status == httpx.codes.UNAUTHORIZED:
        return _LOGIN_HINT
    lowered = text.lower()
    if any(word in lowered for word in _CERT_WORDS):
        return _CERT_HINT
    if any(word in lowered for word in _REACH_WORDS):
        return _REACH_HINT
    return None


def _checked_body(response: httpx.Response, method: str, path: str, secrets: Iterable[str]) -> Any:
    """The masked answer of *response*; raises :class:`HcxApplianceError` for a refusal."""
    secrets = tuple(secrets)
    if response.is_error:
        text = vendor_message(response, secrets)
        raise HcxApplianceError(
            f"HCX Manager answered {method} {path} with HTTP {response.status_code}",
            http_status=response.status_code,
            upstream_message=text,
            remediation=_hint(response.status_code, text),
        )
    body = json_payload_or_empty(response)
    refused = _refusal_text(body)
    if refused is not None:
        text = scrub(refused, secrets)[:_VENDOR_TEXT_MAX]
        status = body.get("httpStatusCode") if isinstance(body, dict) else None
        status = status if isinstance(status, int) and status >= 400 else response.status_code
        raise HcxApplianceError(
            f"HCX Manager refused {method} {path} in its answer",
            http_status=status,
            upstream_message=text,
            remediation=_hint(status, text),
        )
    return _mask_sent_secrets(redact_secrets(body))


class ApplianceApi:
    """One open connection to the appliance management API of one HCX Manager."""

    def __init__(
        self,
        connector: HcxConnector,
        target: _ApplianceTarget,
        client: httpx.AsyncClient,
        credentials: dict[str, str],
    ) -> None:
        self._connector = connector
        self._target = target
        self._client = client
        self._auth = "Basic " + base64_text(f"{credentials['username']}:{credentials['password']}")

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """``GET`` *path*; the parsed JSON answer."""
        return await self.call("GET", path, params=params)

    async def get_or_none(self, path: str) -> Any:
        """``GET`` *path*; ``None`` when the Manager has no such call (``404``)."""
        try:
            return await self.get(path)
        except HcxApplianceError as exc:
            if exc.http_status == httpx.codes.NOT_FOUND:
                return None
            raise

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        secrets: Iterable[str] = (),
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> Any:
        """Send one call; the parsed JSON answer, ``{}`` for an empty one.

        Raises :class:`HcxApplianceError` for an error status, with the
        vendor text cut free of *secrets*; also for a ``2xx`` answer that
        says no (:func:`_refusal_text`). A transport error is raised as it
        is. *timeout* replaces the client's 30 s read time for one call.
        """
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        start = flight_recorder_capture.span_start()
        try:
            response = await _read_capped_json_response(
                self._client,
                method,
                path,
                params=params,
                json=json,
                headers=headers,
                extensions=self._connector._request_extensions(self._target),
                timeout=timeout,
            )
        except httpx.TransportError as exc:
            flight_recorder_capture.record_vendor_call_error(start, method=method, exc=exc)
            raise
        flight_recorder_capture.record_vendor_call(
            start,
            method=method,
            request_headers=headers,
            response=response,
            request_body=_mask_sent_secrets(json),
            request_content_type="application/json" if json is not None else None,
            response_body=_mask_sent_secrets(self._connector.trace_response_body(response)),
        )
        return _checked_body(response, method, path, secrets)


async def _appliance_credentials(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike
) -> dict[str, str]:
    """The appliance account: ``extras.appliance_secret_ref``, else the target's own secret."""
    extras = getattr(target, "extras", None)
    ref = extras.get(APPLIANCE_SECRET_EXTRA) if isinstance(extras, dict) else None
    if isinstance(ref, str) and ref.strip():
        return await load_basic_credentials(
            RefTarget(name=target.name, host=target.host, secret_ref=ref.strip()), operator
        )
    return await connector._session_loader(target, operator)


@asynccontextmanager
async def appliance_session(
    connector: HcxConnector, operator: Operator, target: HcxTargetLike
) -> AsyncIterator[ApplianceApi]:
    """Open the appliance API of *target* for one operation, and close it after."""
    credentials = await _appliance_credentials(connector, operator, target)
    view = _ApplianceTarget(target)
    client = await connector._ephemeral_http_client(view)
    try:
        yield ApplianceApi(connector, view, client, credentials)
    finally:
        await client.aclose()
