# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Client for the appliance setup API of an SRM appliance (``/api/rest/configure/v2``).

The setup API is a second API on the same appliance as the Site Recovery
API. It has its own login: ``POST /api/rest/configure/v2/session`` with
HTTP Basic and the appliance's own admin user (not the vCenter single
sign-on account of the target). The answer carries ``session_id``; every
later call sends it in the ``x-dr-session`` header.

Every setup operation takes the admin user as a Vault reference,
``appliance_secret_ref`` (a KV-v2 path with ``username`` and
``password``). MEHO reads it under the operator's identity on each login
and never returns it.

What this module gives the operations:

* :class:`SetupApi` -- one call with the session header. The session id
  is cached per appliance address, Vault reference and operator. A ``401`` drops
  the session, logs in again and sends the call once more, so a login
  that expired between a start and a status read does not fail the read.
* :class:`SetupApiError` -- the vendor's answer was an error. The message
  holds the HTTP status and the vendor's ``message``.
  :class:`SetupApiUnreachableError` -- the appliance did not answer. Its
  ``stage`` says how far the call got: not past the login, no connection
  for the call, or sent with no answer.
* :func:`task_view`, :func:`find_running_task` and :func:`wait_for_task`
  -- the setup API's own tasks (``/tasks``): the status read of a vendor
  job, the "is one running already" check, and the wait of a read that
  the vendor answers with a task.
* :class:`Steps` -- the step list of a multi-step change: what ran, what
  failed with the vendor's error, and what did not run.
* :data:`SETUP_API_CALLS` -- every vendor call of the setup operations.
  The reconcile test checks them against the pinned vendor file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.connectors._shared.vault_creds import (
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors._shared.vcf_auth import basic_auth_header
from meho_backplane.connectors.vmware_shared.dr_rest import SESSION_HEADER
from meho_backplane.flight_recorder import capture as flight_recorder_capture

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.srm.connector import SrmConnector
    from meho_backplane.connectors.vmware_shared.dr_rest import DrTargetLike

__all__ = [
    "SETUP_API_BASE",
    "SETUP_API_CALLS",
    "SetupApi",
    "SetupApiError",
    "SetupApiUnreachableError",
    "SetupCredentialsLoader",
    "SetupSecretLoader",
    "Steps",
    "as_dict",
    "find_running_task",
    "load_param_basic_credentials",
    "load_param_secret",
    "now_iso",
    "parse_ref",
    "task_view",
    "wait_for_task",
]

_log = structlog.get_logger(__name__)

#: Base path of the appliance setup API.
SETUP_API_BASE: Final = "/api/rest/configure/v2"

#: Every vendor call of the setup operations, as ``(method, path)`` relative
#: to :data:`SETUP_API_BASE`.
SETUP_API_CALLS: Final[tuple[tuple[str, str], ...]] = (
    ("POST", "/session"),
    ("GET", "/session"),
    ("GET", "/appliance"),
    ("GET", "/appliance/configuration-manager/configuration"),
    ("PUT", "/appliance/configuration-manager/configuration"),
    ("POST", "/appliance/configuration-manager/actions/validate-connection"),
    ("POST", "/appliance/configuration-manager/actions/check-existing-registrations"),
    ("POST", "/appliance/configuration-manager/actions/list-vc-services"),
    ("POST", "/appliance/certificates/server/actions/probe-ssl-connection"),
    ("GET", "/appliance/settings/network"),
    ("PUT", "/appliance/settings/network/dns"),
    ("POST", "/appliance/settings/network/interfaces/{interface_name}"),
    ("GET", "/appliance/settings/time"),
    ("PUT", "/appliance/settings/time"),
    ("GET", "/appliance/certificates/server"),
    ("POST", "/appliance/certificates/actions/generate-csr"),
    ("POST", "/appliance/certificates/server/actions/update"),
    ("POST", "/appliance/certificates/ca"),
    ("POST", "/appliance/certificates/ca/actions/remove"),
    ("GET", "/storage-replication-adapters"),
    ("POST", "/storage-replication-adapters"),
    ("GET", "/tasks"),
    ("GET", "/tasks/{task_id}"),
)

#: The vendor's task states, mapped to MEHO's words.
_TASK_STATES: Final[dict[str, str]] = {
    "QUEUED": "queued",
    "RUNNING": "running",
    "SUCCESS": "succeeded",
    "ERROR": "failed",
    "CANCELLED": "cancelled",
}
_RUNNING: Final = frozenset({"QUEUED", "RUNNING"})

#: Seconds between two reads of a task in :func:`wait_for_task`. A test may lower it.
TASK_POLL_SECONDS: float = 2.0

SetupCredentialsLoader = Callable[["DrTargetLike", "Operator", str, str], Awaitable[dict[str, str]]]
"""Async callable ``(target, operator, param, secret_ref) -> {"username", "password"}``."""

SetupSecretLoader = Callable[["DrTargetLike", "Operator", str, str], Awaitable[dict[str, object]]]
"""Async callable ``(target, operator, param, secret_ref) -> the secret's fields``."""

#: httpx errors raised before the request left MEHO: no connection, or the
#: pool, proxy or URL refused it. The appliance got nothing.
_NOT_SENT: Final = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


class SetupApiError(RuntimeError):
    """The setup API answered with an error, or a check before a change failed.

    ``status`` is the HTTP status (``None`` for a check MEHO made itself);
    ``vendor_message`` is the vendor's ``message`` field when it sent one.
    """

    def __init__(
        self, message: str, *, status: int | None = None, vendor_message: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.vendor_message = vendor_message


class SetupApiUnreachableError(SetupApiError):
    """The appliance did not answer (connection refused, timeout, TLS failure).

    ``stage`` says how far the call got:

    * ``"login"`` -- the setup API login did not get through. Nothing of
      the call was sent.
    * ``"connect"`` -- no connection for the call (refused, connect
      timeout, TLS failure). The request was not sent.
    * ``"reply"`` -- the request went out, but no answer came back. Whether
      the appliance ran it is unknown.
    """

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class _RefTarget:
    """Target stand-in that carries a Vault reference from the op params."""

    name: str
    host: str
    secret_ref: str | None


def parse_ref(params: dict[str, Any], name: str) -> str:
    """Return the Vault reference in ``params[name]``; raise ``ValueError`` when it is bad."""
    raw = params.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{name} must be the Vault path of a secret")
    if any(ch < " " or ch == "\x7f" for ch in raw):
        raise ValueError(f"{name} contains a control character")
    return raw.strip()


async def load_param_secret(
    target: DrTargetLike, operator: Operator, param: str, secret_ref: str
) -> dict[str, object]:
    """Read the secret at *secret_ref* (an op param) from Vault, under the operator's identity.

    Errors name *param*, never a value.
    """
    stub = _RefTarget(name=target.name, host=target.host, secret_ref=secret_ref)
    return await load_param_secret_data(stub, operator, param=param)


async def load_param_basic_credentials(
    target: DrTargetLike, operator: Operator, param: str, secret_ref: str
) -> dict[str, str]:
    """Read ``username`` and ``password`` at *secret_ref* (an op param) from Vault.

    Errors name *param*, never a value.
    """
    data = await load_param_secret(target, operator, param, secret_ref)
    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError(f"{param}: the secret needs the string fields username and password")
    creds = {
        "username": strip_credential_value(username),
        "password": strip_credential_value(password),
    }
    if not creds["username"] or not creds["password"]:
        raise ValueError(f"{param}: username or password is empty")
    return creds


def as_dict(value: Any) -> dict[str, Any]:
    """*value* when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def now_iso() -> str:
    """The current time, UTC, ISO 8601."""
    return datetime.now(UTC).isoformat()


def _vendor_message(resp: httpx.Response) -> str | None:
    try:
        payload = resp.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error_code")
        if isinstance(message, str) and message:
            return message[:500]
    return None


class SetupApi:
    """One appliance's setup API, logged in with one admin reference."""

    def __init__(
        self,
        connector: SrmConnector,
        operator: Operator,
        target: DrTargetLike,
        secret_ref: str,
        *,
        param: str = "appliance_secret_ref",
    ) -> None:
        self._connector = connector
        self._operator = operator
        self._target = target
        self._secret_ref = secret_ref
        self._param = param
        # The operator is in the key: each operator reads the Vault reference
        # under their own identity and holds their own login.
        self._key = (
            str(target.tenant_id),
            str(target.host),
            str(target.port),
            secret_ref,
            operator.sub,
        )

    async def _login(self, client: httpx.AsyncClient) -> str:
        path = f"{SETUP_API_BASE}/session"
        creds = await self._connector.setup_credentials_loader(
            self._target, self._operator, self._param, self._secret_ref
        )
        authorization = basic_auth_header(creds["username"], creds["password"])
        try:
            resp = await client.post(
                path,
                headers={"Authorization": authorization, "Accept": "application/json"},
                extensions=self._connector._request_extensions(self._target),
            )
        except httpx.TransportError as exc:
            raise SetupApiUnreachableError(
                f"the appliance did not answer the setup API login ({type(exc).__name__})",
                stage="login",
            ) from None
        if resp.status_code in (401, 403):
            raise SetupApiError(
                f"the setup API login with {self._param} was refused (HTTP {resp.status_code}); "
                "check the appliance admin user and password in Vault",
                status=resp.status_code,
            )
        if not resp.is_success:
            raise SetupApiError(
                f"the setup API login answered HTTP {resp.status_code}",
                status=resp.status_code,
                vendor_message=_vendor_message(resp),
            )
        try:
            session_id = resp.json().get("session_id")
        except (ValueError, AttributeError):
            session_id = None
        if not isinstance(session_id, str) or not session_id:
            raise SetupApiError("the setup API login answered with no session_id")
        return session_id

    async def _session(self, client: httpx.AsyncClient) -> str:
        sessions = self._connector.setup_sessions
        async with self._connector.setup_lock:
            cached = sessions.get(self._key)
            if cached is not None:
                return cached
            session_id = await self._login(client)
            sessions[self._key] = session_id
            _log.info("srm_setup_session_established", target=self._target.name)
            return session_id

    async def _drop(self, session_id: str) -> None:
        async with self._connector.setup_lock:
            if self._connector.setup_sessions.get(self._key) == session_id:
                del self._connector.setup_sessions[self._key]

    async def forget_session(self) -> None:
        """Drop the cached session (for example after the appliance address changed)."""
        async with self._connector.setup_lock:
            self._connector.setup_sessions.pop(self._key, None)

    async def send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        files: dict[str, Any] | None = None,
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        """Send one call; log in again once on ``401``. Return the 2xx answer.

        Raises :class:`SetupApiError` for an error answer and
        :class:`SetupApiUnreachableError` when the appliance does not answer.
        """
        client = await self._connector._http_client(self._target)
        full_path = f"{SETUP_API_BASE}{path}"
        resp: httpx.Response | None = None
        for _attempt in range(2):
            session_id = await self._session(client)
            headers = {SESSION_HEADER: session_id, "Accept": "application/json"}
            span = flight_recorder_capture.span_start()
            try:
                resp = await client.request(
                    method,
                    full_path,
                    headers=headers,
                    json=json,
                    files=files,
                    timeout=timeout,
                    extensions=self._connector._request_extensions(self._target),
                )
            except httpx.TransportError as exc:
                flight_recorder_capture.record_vendor_call_error(span, method=method, exc=exc)
                sent = not isinstance(exc, _NOT_SENT)
                what = "gave no answer" if sent else "could not be reached"
                raise SetupApiUnreachableError(
                    f"{method} {path}: the appliance {what} ({type(exc).__name__})",
                    stage="reply" if sent else "connect",
                ) from None
            # No request body in the trace: register and the server certificate
            # send the SSO admin password in it.
            flight_recorder_capture.record_vendor_call(
                span, method=method, request_headers=headers, response=resp
            )
            if resp.status_code != httpx.codes.UNAUTHORIZED:
                break
            await self._drop(session_id)
        assert resp is not None
        if not resp.is_success:
            vendor = _vendor_message(resp)
            detail = f": {vendor}" if vendor else ""
            raise SetupApiError(
                f"{method} {path} answered HTTP {resp.status_code}{detail}",
                status=resp.status_code,
                vendor_message=vendor,
            )
        return resp

    async def call(self, method: str, path: str, *, json: Any = None) -> Any:
        """Send one call and return its JSON answer (``{}`` for an empty answer)."""
        resp = await self.send(method, path, json=json)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise SetupApiError(f"{method} {path} answered with a body that is not JSON") from None


def _list_of(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("list") if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def task_view(task: Any) -> dict[str, Any]:
    """Reduce a vendor task to the status-read answer fields."""
    data = as_dict(task)
    vendor_status = data.get("status")
    error = data.get("error") if isinstance(data.get("error"), dict) else None
    result = data.get("result")
    return {
        "task_id": data.get("id"),
        "state": _TASK_STATES.get(str(vendor_status), "unknown"),
        "vendor_status": vendor_status,
        "progress": data.get("progress"),
        "type": data.get("type"),
        "description": data.get("description"),
        "error": (
            None
            if error is None
            else {"message": error.get("message"), "details": error.get("details")}
        ),
        "result": None if result is None else scrub_secret_named_values(result)[0],
        "start_time": data.get("start_time"),
        "complete_time": data.get("complete_time"),
    }


async def find_running_task(api: SetupApi, words: Iterable[str]) -> dict[str, Any] | None:
    """Return a queued or running task whose type or description has one of *words*.

    A help, not a lock: two callers can both find nothing and both start.
    """
    lowered = tuple(word.lower() for word in words)
    for task in _list_of(await api.call("GET", "/tasks")):
        if task.get("status") not in _RUNNING:
            continue
        text = f"{task.get('type') or ''} {task.get('description') or ''}".lower()
        if any(word in text for word in lowered):
            return task
    return None


async def wait_for_task(api: SetupApi, task: Any, *, timeout_seconds: float) -> dict[str, Any]:
    """Read *task* until it ends; return the last read. Raise when it fails or runs too long."""
    current = task if isinstance(task, dict) else {}
    task_id = current.get("id")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while current.get("status") in _RUNNING or current.get("status") is None:
        if not isinstance(task_id, str) or not task_id:
            raise SetupApiError("the setup API answered with a task that has no id")
        if loop.time() >= deadline:
            raise SetupApiError(f"task {task_id} did not end within {int(timeout_seconds)} s")
        await asyncio.sleep(TASK_POLL_SECONDS)
        read = await api.call("GET", f"/tasks/{task_id}")
        current = read if isinstance(read, dict) else {}
    if current.get("status") != "SUCCESS":
        vendor = as_dict(current.get("error")).get("message")
        detail = f": {vendor}" if vendor else ""
        raise SetupApiError(
            f"task {task_id} ended with {current.get('status')}{detail}", vendor_message=vendor
        )
    return current


class Steps:
    """The step list of a multi-step change.

    Each row is ``{"step", "status"}`` with ``status`` one of ``done``,
    ``unconfirmed``, ``failed`` and ``not_run``. A failed row has
    ``error``; an unconfirmed row has ``note``.
    """

    def __init__(self, names: Iterable[str]) -> None:
        self._rows: dict[str, dict[str, Any]] = {
            name: {"step": name, "status": "not_run"} for name in names
        }
        self.current: str | None = None

    def start(self, name: str) -> None:
        """Say that *name* runs now; :meth:`fail` marks it."""
        self.current = name

    def done(self, name: str, **info: Any) -> None:
        """Mark *name* as run."""
        self._rows[name].update(status="done", **info)

    def failed(self, name: str, error: str) -> None:
        """Mark *name* as failed with the vendor's or MEHO's error text."""
        self._rows[name].update(status="failed", error=error)

    def unconfirmed(self, name: str, note: str) -> None:
        """Mark *name* as sent with no answer: whether it ran is unknown."""
        self._rows[name].update(status="unconfirmed", note=note)

    def fail(self, error: str) -> None:
        """Mark the step that runs now as failed."""
        if self.current is not None:
            self.failed(self.current, error)

    def skip(self, name: str) -> None:
        """Drop *name*: it does not apply to this call."""
        self._rows.pop(name, None)

    def as_list(self) -> list[dict[str, Any]]:
        """The rows in order."""
        return list(self._rows.values())
