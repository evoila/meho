# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fleet certificate and password handlers for :class:`VcfOperationsConnector`.

In VCF 9.1, the fleet certificates and passwords move into the VCF Operations
API (``/suite-api/api/fleet-management/...``). These handlers read and change
them. The calls exist on VCF Operations 9.1 and newer only. Each handler first
checks the target's version and stops on an older one with a clear message,
before any vendor call
(:func:`~meho_backplane.connectors.vcf_operations.fleet_secrets_checks.require_fleet_api`).

* ``vrops.fleet.certificate.list`` — ``POST .../certificates/query``, or
  ``GET .../certificates/{certificateId}`` for one certificate.
* ``vrops.fleet.certificate.replace`` — ``PUT .../certificates/{certificateId}``.
* ``vrops.fleet.password.list`` — ``POST .../accounts/query``. Only the fields in
  :data:`PASSWORD_ROW_FIELDS` come back, so no password value can.
* ``vrops.fleet.password.rotate`` — ``PUT .../accounts/{passwordAccountKey}/password``.
  The current and the new password come from the secret store
  (``<vault-path>#<field>`` references); MEHO reads them only when the approved
  call runs.
* ``vrops.fleet.request.get`` — ``GET /suite-api/api/workflows/requests/{requestId}``.

The two changes are vendor jobs: the vendor answers with a workflow request
and does the work after that. The start returns ``status: accepted`` with the
request id. The vendor has no list of running fleet requests, so a start
cannot look for one by itself. When the caller passes the id of an earlier
request (``previous_request_id``) and that request still runs, the start
returns it and starts nothing.

The paths come from the vendor's VCF Operations 9.1.0 and 9.1.1 API files.
The op metadata lives in
:mod:`~meho_backplane.connectors.vcf_operations.fleet_secrets_ops`.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors._shared.vcf_auth import SessionLoginError
from meho_backplane.connectors.adapters.http import SsrfBlockedError
from meho_backplane.connectors.vcf_operations.fleet_secrets_checks import (
    ENDED_OUTCOMES,
    parse_password_ref,
    request_outcome,
    require_fleet_api,
)

if TYPE_CHECKING:
    # The concrete connector also inherits HttpConnector; naming it as the
    # base here only lets the type checker see the request seams.
    from meho_backplane.connectors.adapters.http import HttpConnector
    from meho_backplane.connectors.vcf_operations.session import VcfOperationsTargetLike

    _Base = HttpConnector
else:
    _Base = object

__all__ = [
    "CERTIFICATE_PATH",
    "CERTIFICATE_QUERY_PATH",
    "CERTIFICATE_ROW_FIELDS",
    "FLEET_DEFAULT_PAGE_SIZE",
    "PASSWORD_QUERY_PATH",
    "PASSWORD_ROTATE_PATH",
    "PASSWORD_ROW_FIELDS",
    "VENDOR_CALLS",
    "WORKFLOW_REQUEST_PATH",
    "VropsFleetSecretsHandlers",
]

_FLEET = "/suite-api/api/fleet-management"
CERTIFICATE_QUERY_PATH: Final = f"{_FLEET}/certificate-management/certificates/query"
CERTIFICATE_PATH: Final = f"{_FLEET}/certificate-management/certificates/{{certificateId}}"
PASSWORD_QUERY_PATH: Final = f"{_FLEET}/password-management/accounts/query"
PASSWORD_ROTATE_PATH: Final = (
    f"{_FLEET}/password-management/accounts/{{passwordAccountKey}}/password"
)
WORKFLOW_REQUEST_PATH: Final = "/suite-api/api/workflows/requests/{requestId}"

#: Every vendor call these handlers send, as ``METHOD:/path``. The reconcile
#: lane checks each one against the pinned VCF Operations 9.1 API files.
VENDOR_CALLS: Final[tuple[str, ...]] = (
    f"POST:{CERTIFICATE_QUERY_PATH}",
    f"GET:{CERTIFICATE_PATH}",
    f"PUT:{CERTIFICATE_PATH}",
    f"POST:{PASSWORD_QUERY_PATH}",
    f"PUT:{PASSWORD_ROTATE_PATH}",
    f"GET:{WORKFLOW_REQUEST_PATH}",
)

#: Sent as ``pageSize`` when the caller gives none. The vendor default is 10.
FLEET_DEFAULT_PAGE_SIZE: Final = 1000

#: The certificate fields a row keeps (vendor names). Nested objects are
#: flattened: ``autoRenewInfo.autoRenewStatus`` becomes ``autoRenewStatus``.
CERTIFICATE_ROW_FIELDS: Final[tuple[str, ...]] = (
    "certificateResourceKey",
    "appliance",
    "applianceFqdn",
    "displayApplianceType",
    "vcfComponent",
    "vcfEndpoint",
    "domainId",
    "category",
    "type",
    "status",
    "issuedToCommonName",
    "issuedBy",
    "expiryDate",
    "daysToExpire",
    "autoRenewState",
)

#: The password-account fields a row keeps (vendor names). Only these fields
#: are copied, so a password value the vendor might add can never come back.
PASSWORD_ROW_FIELDS: Final[tuple[str, ...]] = (
    "passwordAccountKey",
    "userName",
    "accountType",
    "credentialType",
    "appliance",
    "applianceFqdn",
    "displayApplianceType",
    "applianceOpsResourceId",
    "vcfDomainId",
    "status",
    "expiryDate",
)

_UNREACHABLE_STATUSES: Final = frozenset({502, 503, 504})
_STATUS_READ: Final = "vrops.fleet.request.get"


def _segment(name: str, value: Any) -> str:
    """Check one path value and return it URL-encoded (no ``/``, no dot segment)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value in (".", "..") or any(ch < " " or ch == "\x7f" for ch in value):
        raise ValueError(f"{name} is not a valid id")
    return quote(value, safe="")


@dataclass(slots=True)
class _RefTarget:
    """Points the secret-store read at a reference from the op params.

    Carries the real target's ``name`` / ``host`` for the log line. Not frozen:
    the structural protocol of the read declares settable members.
    """

    name: str
    host: str
    secret_ref: str | None


def _is_not_reachable(exc: Exception) -> bool:
    """True when VCF Operations did not answer (down, or restarting for an upgrade).

    An untrusted certificate means the server did answer, and a destination
    MEHO refused itself was never asked: both go on to the dispatcher.
    """
    if isinstance(exc, httpx.TransportError):
        if isinstance(exc, SsrfBlockedError):
            return False
        return not (
            isinstance(exc.__cause__, ssl.SSLCertVerificationError)
            or "CERTIFICATE_VERIFY_FAILED" in str(exc)
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _UNREACHABLE_STATUSES
    if isinstance(exc, SessionLoginError):
        cause = exc.__cause__
        return isinstance(cause, (httpx.TransportError, httpx.HTTPStatusError)) and (
            _is_not_reachable(cause)
        )
    return False


def _certificate_row(item: Any) -> dict[str, Any]:
    data = item if isinstance(item, dict) else {}
    row = {key: data.get(key) for key in CERTIFICATE_ROW_FIELDS}
    auto = data.get("autoRenewInfo")
    row["autoRenewStatus"] = auto.get("autoRenewStatus") if isinstance(auto, dict) else None
    return row


def _password_row(item: Any) -> dict[str, Any]:
    data = item if isinstance(item, dict) else {}
    return {key: data.get(key) for key in PASSWORD_ROW_FIELDS}


def _total_count(payload: dict[str, Any], rows: list[dict[str, Any]]) -> Any:
    info = payload.get("pageInfo")
    if isinstance(info, dict) and isinstance(info.get("totalCount"), int):
        return info["totalCount"]
    return len(rows)


def _error_text(payload: dict[str, Any]) -> str | None:
    """Join the vendor's error messages into one text, or ``None``."""
    causes = payload.get("errorCause")
    if not isinstance(causes, list):
        return None
    parts: list[str] = []
    for cause in causes:
        if not isinstance(cause, dict):
            continue
        message = cause.get("message")
        detail = cause.get("exceptionMessage")
        text = " — ".join(str(part) for part in (message, detail) if part)
        if text:
            parts.append(text)
    return "; ".join(parts) or None


def _request_view(payload: dict[str, Any], *, request_id: Any, checked_at: str) -> dict[str, Any]:
    state = payload.get("state")
    outcome = request_outcome(state)
    return {
        "request_id": payload.get("requestId") or request_id,
        "reachable": True,
        "state": state,
        "outcome": outcome,
        "ended": outcome in ENDED_OUTCOMES,
        "request_name": payload.get("requestName"),
        "request_type": payload.get("requestType"),
        "category": payload.get("category"),
        "duration": payload.get("duration"),
        "error_text": _error_text(payload),
        "checked_at": checked_at,
    }


def _accepted(payload: Any, *, op_id: str) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    request_id = data.get("requestId")
    note = (
        f"The vendor accepted the job; it is not done yet. Read {_STATUS_READ} "
        "with this request_id until ended is true."
    )
    if not request_id:
        note = (
            "The vendor accepted the call but sent no request id. Do not start again: "
            "read the object with its list operation to see whether it changed."
        )
    return {
        "status": "accepted",
        "started": True,
        "request_id": request_id,
        "state": data.get("state"),
        "request_type": data.get("requestType"),
        "status_read": _STATUS_READ,
        "op_id": op_id,
        "note": note,
    }


class VropsFleetSecretsHandlers(_Base):
    """The five fleet certificate and password handlers of the VCF Operations connector."""

    async def _fleet_query(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        path: str,
        params: dict[str, Any],
        filters: tuple[str, ...],
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            "page": params.get("page", 0),
            "pageSize": params.get("pageSize", FLEET_DEFAULT_PAGE_SIZE),
        }
        for key in ("sortBy", "sortOrder"):
            if params.get(key) is not None:
                query[key] = params[key]
        body = {key: params[key] for key in filters if params.get(key) is not None}
        payload = await self._post_json(target, path, operator=operator, params=query, json=body)
        return payload if isinstance(payload, dict) else {}

    async def _read_request(
        self, operator: Operator, target: VcfOperationsTargetLike, request_id: Any
    ) -> dict[str, Any]:
        path = WORKFLOW_REQUEST_PATH.format(requestId=_segment("request_id", request_id))
        payload = await self._get_json(target, path, operator=operator)
        return payload if isinstance(payload, dict) else {}

    async def _read_request_relogin(
        self, operator: Operator, target: VcfOperationsTargetLike, request_id: Any
    ) -> dict[str, Any]:
        """Read one request; on a 401, log in again and read once more."""
        try:
            return await self._read_request(operator, target, request_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
            # The connector's own session hook (it is not on HttpConnector).
            await getattr(self, "invalidate_session")(target, rejected=exc.request)  # noqa: B009
        return await self._read_request(operator, target, request_id)

    async def _still_running(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
        *,
        op_id: str,
    ) -> dict[str, Any] | None:
        """Return the earlier request when it still runs, else ``None``.

        A state MEHO does not know counts as running: for a change that waits
        for approval, starting nothing is the safe choice.
        """
        previous = params.get("previous_request_id")
        if previous is None:
            return None
        checked_at = datetime.now(UTC).isoformat()
        view = _request_view(
            await self._read_request_relogin(operator, target, previous),
            request_id=previous,
            checked_at=checked_at,
        )
        if view["ended"]:
            return None
        return {
            "status": "already_running",
            "started": False,
            "request_id": view["request_id"],
            "state": view["state"],
            "request_type": view["request_type"],
            "status_read": _STATUS_READ,
            "op_id": op_id,
            "note": (
                "The earlier request has not ended, so MEHO started nothing. Read "
                f"{_STATUS_READ} with this request_id until ended is true."
            ),
        }

    async def _read_password_ref(
        self, operator: Operator, target: VcfOperationsTargetLike, name: str, raw: Any
    ) -> str:
        path, field = parse_password_ref(name, raw)
        data = await load_param_secret_data(
            _RefTarget(name=target.name, host=target.host, secret_ref=path),
            operator,
            param=name,
        )
        value = data.get(field) if isinstance(data, dict) else None
        if not isinstance(value, str) or not strip_credential_value(value):
            raise ValueError(f"{name}: the field {field!r} at {path!r} holds no usable text")
        return strip_credential_value(value)

    async def fleet_certificate_list(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.certificate.list`` — the query, or one certificate by id."""
        require_fleet_api(target, op_id="vrops.fleet.certificate.list")
        certificate_id = params.get("certificateId")
        if certificate_id is not None:
            path = CERTIFICATE_PATH.format(certificateId=_segment("certificateId", certificate_id))
            one = await self._get_json(target, path, operator=operator)
            rows = [_certificate_row(one)]
            return {"certificates": rows, "total_count": 1}
        payload = await self._fleet_query(
            operator,
            target,
            CERTIFICATE_QUERY_PATH,
            params,
            ("appliance", "applianceFqdn", "category", "status", "type"),
        )
        items = payload.get("vcfCertificateModels")
        rows = [_certificate_row(item) for item in items] if isinstance(items, list) else []
        return {"certificates": rows, "total_count": _total_count(payload, rows)}

    async def fleet_certificate_replace(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.certificate.replace`` — start the replacement; returns the request."""
        op_id = "vrops.fleet.certificate.replace"
        require_fleet_api(target, op_id=op_id)
        certificate_id = _segment("certificateId", params.get("certificateId"))
        chain = params.get("certificateChain")
        if isinstance(chain, str) and "PRIVATE KEY" in chain:
            raise ValueError(
                "certificateChain holds a private key. Send only the certificates; "
                "the key stays where the CSR was made."
            )
        running = await self._still_running(operator, target, params, op_id=op_id)
        if running is not None:
            return running
        body: dict[str, Any] = {"caType": params["caType"]}
        if chain:
            body["certificateChain"] = chain
        payload = await self._post_json(
            target,
            CERTIFICATE_PATH.format(certificateId=certificate_id),
            operator=operator,
            verb="PUT",
            json=body,
        )
        return _accepted(payload, op_id=op_id)

    async def fleet_password_list(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.password.list`` — the password accounts, never a value."""
        require_fleet_api(target, op_id="vrops.fleet.password.list")
        payload = await self._fleet_query(
            operator,
            target,
            PASSWORD_QUERY_PATH,
            params,
            ("appliance", "applianceFqdn", "status", "username", "vcfDomainId"),
        )
        items = payload.get("vcfPasswordAccounts")
        rows = [_password_row(item) for item in items] if isinstance(items, list) else []
        return {"accounts": rows, "total_count": _total_count(payload, rows)}

    async def fleet_password_rotate(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.password.rotate`` — start the change; returns the request.

        Both passwords are read from the secret store now, under the
        operator's identity, and go only into the request body.
        """
        op_id = "vrops.fleet.password.rotate"
        require_fleet_api(target, op_id=op_id)
        account_key = _segment("passwordAccountKey", params.get("passwordAccountKey"))
        parse_password_ref("current_password_ref", params.get("current_password_ref"))
        parse_password_ref("new_password_ref", params.get("new_password_ref"))
        running = await self._still_running(operator, target, params, op_id=op_id)
        if running is not None:
            return running
        body: dict[str, Any] = {
            "currentPassword": await self._read_password_ref(
                operator, target, "current_password_ref", params["current_password_ref"]
            ),
            "newPassword": await self._read_password_ref(
                operator, target, "new_password_ref", params["new_password_ref"]
            ),
        }
        if params.get("userName"):
            body["userName"] = params["userName"]
        payload = await self._post_json(
            target,
            PASSWORD_ROTATE_PATH.format(passwordAccountKey=account_key),
            operator=operator,
            verb="PUT",
            json=body,
        )
        return _accepted(payload, op_id=op_id)

    async def fleet_request_get(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.fleet.request.get`` — one fleet request: state, outcome, error text.

        When VCF Operations does not answer, returns ``reachable: false`` and
        ``outcome: not_reachable`` with the request id and the time; it never
        says ``failed`` then. When the login expired since the start (a 401),
        it drops the old session, logs in again and reads once more.
        """
        require_fleet_api(target, op_id=_STATUS_READ)
        request_id = params.get("request_id")
        checked_at = datetime.now(UTC).isoformat()
        try:
            payload = await self._read_request_relogin(operator, target, request_id)
        except (httpx.TransportError, httpx.HTTPStatusError, SessionLoginError) as exc:
            if not _is_not_reachable(exc):
                raise
            return {
                "request_id": request_id,
                "reachable": False,
                "state": None,
                "outcome": "not_reachable",
                "ended": False,
                "error_text": None,
                "checked_at": checked_at,
                "note": (
                    "VCF Operations did not answer. The job may still run. Read again "
                    "later with the same request_id."
                ),
            }
        return _request_view(payload, request_id=request_id, checked_at=checked_at)
