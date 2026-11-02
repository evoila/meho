# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Lifecycle requests, one environment, inventory sync, health and datacenters on ``/lcm``.

The handlers of seven typed operations of the ``fleet-rest`` connector
(Aria Suite Lifecycle 8.x and VCF 9.0 fleet management). They live on
:class:`FleetLifecycleOps`, a base class of
:class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`,
so the dispatcher binds them to the connector like its other handlers.
The operation metadata is in :mod:`.lifecycle_ops`.

**Requests.** Every change on the appliance runs as a request. The
vendor's request state comes back as ``state``, and ``outcome`` puts it
in plain words (:data:`OUTCOME_BY_STATE`): ``queued``, ``running``,
``succeeded``, ``failed`` or ``cancelled``; a state that is not in the
table gives ``unknown``. The vendor gives no progress number and no list
of steps, so the request read returns the error causes of a failed
request instead: the first one names the step that failed.

**Not reachable.** When the appliance does not answer (a connection or
time-out error, or a 502, 503 or 504 from the proxy in front of it, for
example during its own upgrade), ``fleet.request.get`` returns
``outcome: not_reachable`` with the request id and the time. It does not
say ``failed``. Both reads of the operation are covered: when the request
read worked and the error-causes read did not, ``state`` keeps what the
request read said and ``outcome`` is still ``not_reachable``. Two errors
are not "not reachable" and go on to the dispatcher: a certificate MEHO
does not trust, and a destination MEHO refused before any request.

**Login again.** The connector sends the stored user and password with
every call. When the appliance answers ``401``, the request read and the
two changes drop the cached login, read it again and repeat the call
once. A ``401`` means the appliance refused the call before it ran, so
the repeat cannot run a change twice.

**Retry steps.** ``fleet.request.retry`` runs three steps: read the
request, read its error causes, send the retry. A vendor or transport
error at any step ends the operation with ``status: failed``; ``steps``
then says which steps ran, which one failed (with the vendor's error) and
which did not run. Nothing is undone.

**Secrets.** MEHO's shared secret-field scrub
(:func:`~meho_backplane.redaction.secret_fields.scrub_secret_fields`)
runs on every vendor object an answer carries and on every vendor text
(error causes, request reasons and error texts). A field whose name is a
secret name (:func:`~meho_backplane.broadcast.events.is_secret_param_name`)
is replaced by :data:`REDACTED`, at any depth, also when it sits as a
quoted pair inside a text such as ``"adminPassword": "..."``. The
datacenter list builds its rows from named fields only, so the vCenter
password the vendor sends never reaches the answer.
"""

from __future__ import annotations

import ssl
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.adapters.http import HttpConnector, SsrfBlockedError
from meho_backplane.connectors.vcf_fleet.session import VcfFleetTargetLike
from meho_backplane.redaction.secret_fields import (
    SECRET_FIELD_MARKER,
    scrub_secret_fields,
    scrub_secret_fields_in_text,
)

__all__ = [
    "OUTCOME_BY_STATE",
    "REDACTED",
    "VENDOR_CALLS",
    "FleetLifecycleOps",
]

_REQUESTS_PATH: Final = "/lcm/request/api/v2/requests"
_REQUEST_PATH: Final = "/lcm/request/api/v2/requests/{requestId}"
_REQUEST_ERROR_CAUSES_PATH: Final = "/lcm/request/api/v2/requests/{requestId}/error-causes"
_REQUEST_RETRY_PATH: Final = "/lcm/request/api/v2/requests/{requestId}/retry"
_ENVIRONMENT_PATH: Final = "/lcm/lcops/api/v2/environments/{environmentId}"
_PRODUCT_INVENTORY_SYNC_PATH: Final = (
    "/lcm/lcops/api/v2/environments/{environmentId}/products/{productId}/inventory-sync"
)
_SYSTEM_DETAILS_PATH: Final = "/lcm/lcops/api/v2/settings/system-details"
_HEALTH_STATUS_PATH: Final = "/lcm/health/api/v2/status"
_DATACENTERS_PATH: Final = "/lcm/lcops/api/v2/datacenters"
_DATACENTER_VCENTERS_PATH: Final = "/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters"

#: Every vendor call of this module, as ``(method, path)``. The reconcile
#: lane checks each one against the pinned ``/lcm`` API file.
VENDOR_CALLS: Final[tuple[tuple[str, str], ...]] = (
    ("GET", _REQUESTS_PATH),
    ("GET", _REQUEST_PATH),
    ("GET", _REQUEST_ERROR_CAUSES_PATH),
    ("PATCH", _REQUEST_RETRY_PATH),
    ("GET", _ENVIRONMENT_PATH),
    ("POST", _PRODUCT_INVENTORY_SYNC_PATH),
    ("GET", _SYSTEM_DETAILS_PATH),
    ("GET", _HEALTH_STATUS_PATH),
    ("GET", _DATACENTERS_PATH),
    ("GET", _DATACENTER_VCENTERS_PATH),
)

#: The text in place of a secret value: the marker of MEHO's shared scrub.
REDACTED: Final = SECRET_FIELD_MARKER

#: The vendor request state (lower case, no ``_`` or ``-``) in plain words.
OUTCOME_BY_STATE: Final[dict[str, str]] = {
    "created": "queued",
    "scheduled": "queued",
    "pending": "queued",
    "inprogress": "running",
    "running": "running",
    "completed": "succeeded",
    "success": "succeeded",
    "succeeded": "succeeded",
    "failed": "failed",
    "failure": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
_ACTIVE_OUTCOMES: Final = frozenset({"queued", "running"})
_NOT_REACHABLE: Final = "not_reachable"
_UNKNOWN: Final = "unknown"
_STATUS_READ: Final = "fleet.request.get"
_GATEWAY_STATUSES: Final = frozenset({502, 503, 504})
_ERROR_CAUSE_MAX: Final = 2000
_ERROR_TEXT_MAX: Final = 500
_LIST_LIMIT_DEFAULT: Final = 100
_VENDOR_ERRORS: Final = (httpx.HTTPStatusError, httpx.TransportError)


def _segment(kind: str, value: Any) -> str:
    """Return *value* as one URL path segment, or raise ``ValueError``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{kind} must be a non-empty string, got {value!r}")
    if value in {".", ".."}:
        raise ValueError(f"{kind} must not be {value!r}")
    return quote(value, safe="")


def _plain(text: str) -> str:
    """*text* in lower case without ``_`` and ``-``, for a loose name compare."""
    return text.strip().lower().replace("_", "").replace("-", "")


def _outcome(state: Any) -> str:
    if not isinstance(state, str):
        return _UNKNOWN
    return OUTCOME_BY_STATE.get(_plain(state), _UNKNOWN)


def _vendor_text(text: Any, limit: int = _ERROR_CAUSE_MAX) -> Any:
    """A vendor text for an answer: secret-named values replaced, then cut at *limit*."""
    if not isinstance(text, str):
        return text
    return scrub_secret_fields_in_text(text)[:limit]


def _is_not_reachable(exc: Exception) -> bool:
    """True when the appliance did not answer; False for an error MEHO must report."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _GATEWAY_STATUSES
    if not isinstance(exc, httpx.TransportError) or isinstance(exc, SsrfBlockedError):
        return False
    return not (
        isinstance(exc.__cause__, ssl.SSLCertVerificationError)
        or "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )


def _vendor_error(exc: Exception) -> str:
    """The vendor's error text for a failed call, short, with secret values replaced."""
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        try:
            body = response.json()
        except ValueError:
            body = None
        text = response.text
        if isinstance(body, dict):
            parts = [str(body[key]) for key in ("errorCode", "message") if body.get(key)]
            if parts:
                text = " ".join(parts)
        return f"HTTP {response.status_code}: {_vendor_text(text, _ERROR_TEXT_MAX)}"
    return str(_vendor_text(f"{type(exc).__name__}: {exc}", _ERROR_TEXT_MAX))


def _request_row(item: dict[str, Any]) -> dict[str, Any]:
    """One request as MEHO returns it.

    Of the request inputs only the environment and product ids are returned,
    as ``environment_id`` and ``product_id``. A caller recovering a lost reply
    needs them to find its request among others of the same type. The other
    inputs stay out: they can hold passwords.
    """
    inputs = item.get("inputMap")
    return {
        "request_id": item.get("vmid"),
        "name": item.get("requestName"),
        "type": item.get("requestType"),
        "reason": _vendor_text(item.get("requestReason")),
        "state": item.get("state"),
        "outcome": _outcome(item.get("state")),
        "execution_status": item.get("executionStatus"),
        "error_cause": _vendor_text(item.get("errorCause")),
        "created_by": item.get("createdBy"),
        "last_updated_on": item.get("lastUpdatedOn"),
        "environment_id": _input_of(inputs, "environmentId"),
        "product_id": _input_of(inputs, "productId"),
    }


def _error_cause_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": item.get("eventId"),
        "message_id": item.get("messageId"),
        "message": _vendor_text(item.get("message")),
        "exception_message": _vendor_text(item.get("exceptionMessage")),
        "retry": item.get("retry"),
        "properties": scrub_secret_fields(item.get("properties") or {}),
    }


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _input_of(input_map: Any, name: str) -> str | None:
    """The string value of the request input *name*; case, ``_`` and ``-`` do not matter."""
    if not isinstance(input_map, dict):
        return None
    wanted = _plain(name)
    for key, value in input_map.items():
        if isinstance(key, str) and isinstance(value, str) and _plain(key) == wanted:
            return value
    return None


def _is_inventory_sync_for(item: dict[str, Any], environment_id: str, product_id: str) -> bool:
    """True for an active inventory sync whose inputs name exactly this environment and product.

    The ids are compared with the ``environmentId`` and ``productId`` inputs
    of the request, each as a whole value. A request whose inputs do not
    carry both ids is not a match, whatever other text its inputs hold.
    """
    if _outcome(item.get("state")) not in _ACTIVE_OUTCOMES:
        return False
    kind = f"{item.get('requestType') or ''}{item.get('requestName') or ''}"
    if "inventorysync" not in _plain(kind):
        return False
    inputs = item.get("inputMap")
    return (
        _input_of(inputs, "environmentId") == environment_id
        and _input_of(inputs, "productId") == product_id
    )


def _failed_at(answer: dict[str, Any], step: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """The retry answer when *step* failed with *exc*: the steps before it stay ``done``."""
    step.update(state="failed", error=_vendor_error(exc))
    return {**answer, "status": "failed", "reason": step["error"]}


class FleetLifecycleOps(HttpConnector):
    """The request, environment, inventory sync, health and datacenter handlers."""

    async def invalidate_credentials(self, target: VcfFleetTargetLike) -> None:
        """Drop the cached login of *target*. The connector class implements it."""
        raise NotImplementedError

    async def _with_relogin(
        self, target: VcfFleetTargetLike, call: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run *call*; on a ``401`` drop the cached login and run it once more."""
        try:
            return await call()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
        await self.invalidate_credentials(target)
        return await call()

    async def _read(self, target: VcfFleetTargetLike, operator: Operator, path: str) -> Any:
        """``GET`` *path* as JSON, with one new login after a ``401``."""
        return await self._with_relogin(
            target, lambda: self._get_json(target, path, operator=operator)
        )

    async def request_list(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.request.list`` -- ``GET /lcm/request/api/v2/requests``, newest first."""
        payload = await self._get_json(target, _REQUESTS_PATH, operator=operator)
        rows = [_request_row(item) for item in _as_list(payload)]
        state = params.get("state")
        if isinstance(state, str) and state.strip():
            wanted = state.strip().lower()
            rows = [row for row in rows if str(row["state"] or "").lower() == wanted]
        outcome = params.get("outcome")
        if isinstance(outcome, str) and outcome:
            rows = [row for row in rows if row["outcome"] == outcome]
        rows.sort(key=lambda row: row["last_updated_on"] or 0, reverse=True)
        limit = int(params.get("limit") or _LIST_LIMIT_DEFAULT)
        return {"requests": rows[:limit], "more_rows": len(rows) > limit}

    async def request_get(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.request.get`` -- one request, and the error causes when it failed."""
        request_id = _segment("request_id", params.get("request_id"))
        base: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "error": None,
            "error_causes": [],
            "failed_step": None,
        }
        row = _request_row({"vmid": params.get("request_id")})
        try:
            payload = await self._read(target, operator, _REQUEST_PATH.format(requestId=request_id))
        except _VENDOR_ERRORS as exc:
            if not _is_not_reachable(exc):
                raise
            return {**row, **base, "outcome": _NOT_REACHABLE, "error": _vendor_error(exc)}
        row = _request_row(payload if isinstance(payload, dict) else {})
        if row["outcome"] != "failed":
            return {**row, **base}
        try:
            raw = await self._read(
                target, operator, _REQUEST_ERROR_CAUSES_PATH.format(requestId=request_id)
            )
        except _VENDOR_ERRORS as exc:
            if not _is_not_reachable(exc):
                raise
            return {**row, **base, "outcome": _NOT_REACHABLE, "error": _vendor_error(exc)}
        causes = [_error_cause_row(item) for item in _as_list(raw)]
        return {**row, **base, "error_causes": causes, "failed_step": causes[0] if causes else None}

    async def request_retry(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.request.retry`` -- read the request and its causes, then ``PATCH .../retry``."""
        request_id = _segment("request_id", params.get("request_id"))
        changes = _property_changes(params.get("properties"))
        steps = [
            {"step": "read_request", "state": "not_run", "error": None},
            {"step": "read_error_causes", "state": "not_run", "error": None},
            {"step": "retry", "state": "not_run", "error": None},
        ]
        answer: dict[str, Any] = {
            "request_id": params.get("request_id"),
            "state": None,
            "outcome": _UNKNOWN,
            "status_read": _STATUS_READ,
            "retried_event_ids": [],
            "steps": steps,
        }
        try:
            request = await self._read(target, operator, _REQUEST_PATH.format(requestId=request_id))
        except _VENDOR_ERRORS as exc:
            return _failed_at(answer, steps[0], exc)
        steps[0]["state"] = "done"
        row = _request_row(request if isinstance(request, dict) else {})
        answer.update(state=row["state"], outcome=row["outcome"])
        if row["outcome"] != "failed":
            return {**answer, "status": "not_started", "reason": "only a failed request is retried"}
        try:
            raw = await self._read(
                target, operator, _REQUEST_ERROR_CAUSES_PATH.format(requestId=request_id)
            )
        except _VENDOR_ERRORS as exc:
            return _failed_at(answer, steps[1], exc)
        steps[1]["state"] = "done"
        causes = _as_list(raw)
        if not causes:
            return {**answer, "status": "not_started", "reason": "the request has no error causes"}
        body: Any = _retry_body(causes, changes)
        try:
            await self._with_relogin(
                target,
                lambda: self._post_json(
                    target,
                    _REQUEST_RETRY_PATH.format(requestId=request_id),
                    operator=operator,
                    verb="PATCH",
                    json=body,
                ),
            )
        except _VENDOR_ERRORS as exc:
            return _failed_at(answer, steps[2], exc)
        steps[2]["state"] = "done"
        event_ids = [cause.get("eventId") for cause in causes]
        return {**answer, "status": "accepted", "reason": None, "retried_event_ids": event_ids}

    async def environment_get(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.environment.get`` -- one environment with its products and nodes."""
        environment_id = _segment("environment_id", params.get("environment_id"))
        payload = await self._get_json(
            target, _ENVIRONMENT_PATH.format(environmentId=environment_id), operator=operator
        )
        result = scrub_secret_fields(payload)
        return result if isinstance(result, dict) else {"environment": result}

    async def product_inventory_sync(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.product.inventory_sync`` -- start an inventory sync unless one runs."""
        environment_id = _segment("environment_id", params.get("environment_id"))
        product_id = _segment("product_id", params.get("product_id"))
        answer: dict[str, Any] = {
            "environment_id": params.get("environment_id"),
            "product_id": params.get("product_id"),
            "status_read": _STATUS_READ,
        }
        requests = await self._get_json(target, _REQUESTS_PATH, operator=operator)
        for item in _as_list(requests):
            if _is_inventory_sync_for(item, answer["environment_id"], answer["product_id"]):
                return {**answer, "status": "already_running", "request_id": item.get("vmid")}
        path = _PRODUCT_INVENTORY_SYNC_PATH.format(
            environmentId=environment_id, productId=product_id
        )
        started = await self._with_relogin(
            target, lambda: self._post_json(target, path, operator=operator)
        )
        return {**answer, "status": "accepted", "request_id": _request_id_of(started)}

    async def health(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.health`` -- appliance details and the state of its services."""
        del params  # schema declares the param object empty
        details, details_error = await self._read_part(target, _SYSTEM_DETAILS_PATH, operator)
        status, status_error = await self._read_part(target, _HEALTH_STATUS_PATH, operator)
        services = [
            {"service": name, "state": state}
            for name, state in (status.items() if isinstance(status, dict) else [])
        ]
        all_up = all(str(row["state"]).upper() == "UP" for row in services) if services else None
        info = details if isinstance(details, dict) else {}
        return {
            "name": info.get("name"),
            "vendor": info.get("vendor"),
            "version": info.get("version"),
            "build_number": info.get("buildNumber"),
            "patch_version": info.get("patchVersion"),
            "total_storage": info.get("totalStorage"),
            "used_storage": info.get("usedStorage"),
            "available_storage": info.get("availableStorage"),
            "storage_percentage": info.get("storagePercentage"),
            "all_up": all_up,
            "services": services,
            "system_details_error": details_error,
            "health_error": status_error,
        }

    async def _read_part(
        self, target: VcfFleetTargetLike, path: str, operator: Operator
    ) -> tuple[Any, str | None]:
        """One read of the health answer; a vendor error other than ``401`` is reported."""
        try:
            return await self._get_json(target, path, operator=operator), None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise
            return None, _vendor_error(exc)

    async def datacenter_list(
        self, operator: Operator, target: VcfFleetTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``fleet.datacenter.list`` -- one row per vCenter, with its datacenter."""
        del params  # schema declares the param object empty
        datacenters = await self._get_json(target, _DATACENTERS_PATH, operator=operator)
        rows: list[dict[str, Any]] = []
        for datacenter in _as_list(datacenters):
            base = {
                "datacenter_id": datacenter.get("dataCenterVmid"),
                "datacenter_name": datacenter.get("dataCenterName"),
                "primary_location": datacenter.get("primaryLocation"),
            }
            vmid = _segment("dataCenterVmid", datacenter.get("dataCenterVmid"))
            vcenters = _as_list(
                await self._get_json(
                    target, _DATACENTER_VCENTERS_PATH.format(dataCenterVmid=vmid), operator=operator
                )
            )
            if not vcenters:
                rows.append({**base, **_vcenter_row({})})
            rows.extend({**base, **_vcenter_row(vcenter)} for vcenter in vcenters)
        return {"rows": rows}


def _vcenter_row(vcenter: dict[str, Any]) -> dict[str, Any]:
    """Named fields only: the vendor's ``vcPassword`` never reaches the answer."""
    return {
        "vcenter_name": vcenter.get("vCenterName"),
        "vcenter_host": vcenter.get("vCenterHost"),
        "vcenter_username": vcenter.get("vcUsername"),
        "used_as": vcenter.get("vcUsedAs"),
        "data_collection_status": vcenter.get("vcDataCollectionStatus"),
    }


def _request_id_of(answer: Any) -> Any:
    """The ``requestId`` of a start answer: an object, or a list of one object."""
    if isinstance(answer, list) and answer:
        answer = answer[0]
    return answer.get("requestId") if isinstance(answer, dict) else None


def _property_changes(raw: Any) -> dict[str, str]:
    """Check the corrected inputs of a retry. A secret goes in as a locker reference only."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("properties must be an object of names and string values")
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ValueError(f"properties.{name} must be a string")
        if is_secret_param_name(name) and not value.startswith("locker:"):
            raise ValueError(
                f"properties.{name} is a secret: give a locker reference "
                "(locker:password:<id>:<alias>), never the value"
            )
    return dict(raw)


def _retry_body(causes: list[dict[str, Any]], changes: dict[str, str]) -> list[dict[str, Any]]:
    """The error causes as the vendor gave them, marked for retry, with the corrected inputs."""
    known = {name for cause in causes for name in (cause.get("properties") or {})}
    unknown = sorted(set(changes) - known)
    if unknown:
        raise ValueError(
            f"properties {unknown} are not inputs of the failed step; known inputs: {sorted(known)}"
        )
    body = []
    for cause in causes:
        properties = dict(cause.get("properties") or {})
        properties.update({name: value for name, value in changes.items() if name in properties})
        body.append({**cause, "retry": True, "properties": properties})
    return body
