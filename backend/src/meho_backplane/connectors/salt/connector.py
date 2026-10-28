# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SaltConnector -- hand-written HttpConnector subclass for the Salt RaaS API.

Covers the RaaS RPC API (also called the eAPI) of Aria Automation Config
8.x (VMware Salt) and of VCF 9.1 Salt, through its HTTP bridge.

Login
-----

The vendor page "API Usage" of the RaaS 8.18 reference:

1. ``GET /account/login`` with HTTP Basic: a RaaS user, read from Vault
   through the :class:`~meho_backplane.connectors.salt.session.SaltSessionLoader`.
   The answer sets cookies, among them ``_xsrf``.
2. Every RPC call is ``POST /rpc`` with the same HTTP Basic, the cookies
   and the header ``X-Xsrftoken: <value of the _xsrf cookie>``. The body is
   ``{"resource": ..., "method": ..., "kwarg": {...}}``.
3. The session (the Basic header, the cookies and the xsrf value) is
   cached per credential (``credential_cache_key``: tenant, secret and
   login host), so two targets that share one credential share one session.
4. On ``401`` from a later call the dispatcher calls
   :meth:`invalidate_session`, which drops the shared session, and
   dispatches once more; the next call logs in again and reads the
   credentials from Vault again.

The answer of ``/rpc`` is ``{"riq": ..., "ret": <result>, "error": ...,
"warnings": [...]}``. :func:`unwrap_rpc` returns ``ret``, or raises
:class:`SaltRpcError` with the vendor's message when ``error`` is set.

Version
-------

``api.get_versions`` returns the versions of the RaaS parts.
:meth:`server_version` reads the ``raas`` version from it and never raises;
it backs :meth:`fingerprint`. The operation ``salt.about`` returns the
versions and the masters RaaS knows.

Operations
----------

Six reads; their bodies live in :mod:`meho_backplane.connectors.salt.reads`.
All exist on every covered version, so no operation needs a version check.

Traces
------

The flight recorder stores each ``/rpc`` answer only after
:meth:`SaltConnector.trace_response_body` has run it through the same
secret rules as the read that made the call, so a value a read withholds
never reaches the stored trace.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import credential_cache_key
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vcf_auth import (
    basic_auth_header,
    is_acceptable_auth_model,
    session_establish_auth_error,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.salt.session import (
    SaltSessionLoader,
    SaltTargetLike,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = [
    "CMDS_RPC",
    "GET_VERSIONS_RPC",
    "MASTER_PRESENCE_RPC",
    "MINION_CACHE_RPC",
    "MINION_KEY_STATE_RPC",
    "MINION_PRESENCE_RPC",
    "RETURNS_RPC",
    "TARGET_GROUP_RPC",
    "SaltConnector",
    "SaltRpcError",
    "SaltServerVersion",
    "parse_server_version",
    "unwrap_rpc",
]

_log = structlog.get_logger(__name__)

#: Login of the HTTP bridge. ``GET`` with HTTP Basic; the answer sets cookies.
LOGIN_PATH: Final[str] = "/account/login"

#: The one RPC path. Every call is a ``POST`` with resource, method and kwarg.
RPC_PATH: Final[str] = "/rpc"

#: Cookie that carries the xsrf value, and the header that sends it back.
_XSRF_COOKIE: Final[str] = "_xsrf"
_XSRF_HEADER: Final[str] = "X-Xsrftoken"

#: The RPC methods the connector calls, as ``(resource, method)``. The
#: reconcile lane reads every module constant whose name ends in ``_RPC``
#: and checks it against the pinned RaaS reference list.
GET_VERSIONS_RPC: Final[tuple[str, str]] = ("api", "get_versions")
MASTER_PRESENCE_RPC: Final[tuple[str, str]] = ("master", "get_master_presence")
MINION_CACHE_RPC: Final[tuple[str, str]] = ("minions", "get_minion_cache")
MINION_PRESENCE_RPC: Final[tuple[str, str]] = ("minions", "get_minion_presence")
MINION_KEY_STATE_RPC: Final[tuple[str, str]] = ("minions", "get_minion_key_state")
CMDS_RPC: Final[tuple[str, str]] = ("cmd", "get_cmds")
RETURNS_RPC: Final[tuple[str, str]] = ("ret", "get_returns")
TARGET_GROUP_RPC: Final[tuple[str, str]] = ("tgt", "get_target_group")

#: Longest vendor error text kept in an error message.
_ERROR_TEXT_LIMIT: Final[int] = 500

_VERSION_PREFIX: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)*")


class SaltRpcError(RuntimeError):
    """The RPC answer carried an ``error``, or had no ``ret``. The message says which."""


@dataclass(frozen=True)
class SaltServerVersion:
    """The RaaS version as :meth:`SaltConnector.server_version` reads it.

    ``version`` is the leading ``X.Y.Z`` part of ``raw`` (what RaaS reports).
    ``error`` names why the read failed; then ``version`` and ``raw`` are
    ``None``.
    """

    version: str | None
    raw: str | None
    error: str | None


@dataclass(frozen=True)
class _SaltSession:
    """One logged-in RaaS session: what every RPC call sends."""

    authorization: str
    cookie: str | None
    xsrf: str | None

    def headers(self) -> dict[str, str]:
        headers = {"Authorization": self.authorization}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.xsrf:
            headers[_XSRF_HEADER] = self.xsrf
        return headers


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= _ERROR_TEXT_LIMIT else text[:_ERROR_TEXT_LIMIT] + "..."


def unwrap_rpc(payload: Any, call: tuple[str, str]) -> Any:
    """Return ``ret`` of an RPC answer; raise :class:`SaltRpcError` on ``error``."""
    name = f"{call[0]}.{call[1]}"
    if not isinstance(payload, dict):
        raise SaltRpcError(f"salt rpc {name}: the answer is not a JSON object")
    error = payload.get("error")
    if error:
        if isinstance(error, dict):
            message = error.get("message") or "no message"
            detail = error.get("detail")
            text = _short(message) + (f" ({_short(detail)})" if detail else "")
        else:
            text = _short(error)
        raise SaltRpcError(f"salt rpc {name} returned an error: {text}")
    if "ret" not in payload:
        raise SaltRpcError(f"salt rpc {name}: the answer has no ret")
    return payload["ret"]


def raas_version_text(ret: Any) -> str | None:
    """The RaaS version text in a ``api.get_versions`` result, or ``None``."""
    if not isinstance(ret, dict):
        return None
    raas = ret.get("raas")
    if isinstance(raas, dict):
        raas = raas.get("raas")
    return raas if isinstance(raas, str) and raas else None


def parse_server_version(ret: Any) -> SaltServerVersion:
    """Pick the RaaS version out of a ``api.get_versions`` result."""
    raw = raas_version_text(ret)
    if raw is None:
        return SaltServerVersion(
            version=None, raw=None, error="api.get_versions has no raas version"
        )
    match = _VERSION_PREFIX.match(raw.strip())
    if match is None:
        return SaltServerVersion(version=None, raw=raw, error=f"raas version {raw!r} has no number")
    return SaltServerVersion(version=match.group(0), raw=raw, error=None)


class SaltConnector(HttpConnector):
    """Salt RaaS RPC connector: HTTP Basic, cookies and ``X-Xsrftoken``."""

    product = "salt"
    version = "8.18"
    impl_id = "salt-raas"
    supported_version_range = ">=8.0,<10.0"
    priority = 1

    def __init__(self, *, session_loader: SaltSessionLoader | None = None) -> None:
        super().__init__()
        self._sessions: dict[tuple[str, ...], _SaltSession] = {}
        self._session_lock = asyncio.Lock()
        self._session_loader: SaltSessionLoader = (
            session_loader if session_loader is not None else load_session_credentials_from_vault
        )

    async def auth_headers(self, target: SaltTargetLike, operator: Operator) -> dict[str, str]:
        """Return the session headers (Basic, cookies, xsrf), logging in on first use.

        Raises :exc:`NotImplementedError` when ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"SaltConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        session = await self._session(target, operator)
        return session.headers()

    async def _session(self, target: SaltTargetLike, operator: Operator) -> _SaltSession:
        """Return the cached session for *target*, logging in on first use."""
        cache_key = credential_cache_key(target)
        async with self._session_lock:
            cached = self._sessions.get(cache_key)
            if cached is not None:
                return cached
            creds = await self._session_loader(target, operator)
            try:
                authorization = basic_auth_header(creds["username"], creds["password"])
            except KeyError as exc:
                raise RuntimeError(
                    f"salt session loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            client = await self._http_client(target)
            try:
                resp = await client.get(
                    LOGIN_PATH,
                    headers={"Authorization": authorization},
                    extensions=self._request_extensions(target),
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                message = (
                    f"salt session establish failed for target {target.name!r}: "
                    f"GET {LOGIN_PATH} returned HTTP {exc.response.status_code}"
                )
                raise (
                    session_establish_auth_error(exc, message=message, target=target)
                    or RuntimeError(message)
                ) from exc
            cookies = {cookie.name: cookie.value for cookie in resp.cookies.jar if cookie.value}
            xsrf = cookies.get(_XSRF_COOKIE) or resp.headers.get(_XSRF_HEADER)
            cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
            session = _SaltSession(
                authorization=authorization, cookie=cookie_header or None, xsrf=xsrf or None
            )
            self._sessions[cache_key] = session
            _log.info(
                "salt_session_established",
                target=target.name,
                host=target.host,
                cookie_names=sorted(cookies),
                xsrf=session.xsrf is not None,
            )
            return session

    async def invalidate_session(self, target: SaltTargetLike) -> None:
        """Drop the cached session of *target*'s credential (the dispatcher's 401 hook).

        The next :meth:`auth_headers` call logs in again and reads the
        credentials through the loader again. The pooled client's cookie
        jar for this target is cleared too.
        """
        cache_key = credential_cache_key(target)
        async with self._session_lock:
            self._sessions.pop(cache_key, None)
            client = self._clients.get(self._client_cache_key(target))
            if client is not None:
                client.cookies.clear()

    async def rpc(
        self,
        target: SaltTargetLike,
        operator: Operator,
        call: tuple[str, str],
        kwarg: dict[str, Any] | None = None,
    ) -> Any:
        """Call one RPC method and return its ``ret``. ``None`` kwarg values are left out."""
        body = {
            "resource": call[0],
            "method": call[1],
            "kwarg": {key: value for key, value in (kwarg or {}).items() if value is not None},
        }
        payload = await self._post_json(
            target,
            RPC_PATH,
            operator=operator,
            json=body,
            extra_headers={"Accept": "application/json"},
        )
        return unwrap_rpc(payload, call)

    async def server_version(self, target: SaltTargetLike, operator: Operator) -> SaltServerVersion:
        """Read the RaaS version. Never raises: a failure is named in ``error``.

        A ``401`` drops the cached session, so the next call logs
        in again instead of sending the rejected session once more.
        """
        try:
            ret = await self.rpc(target, operator, GET_VERSIONS_RPC)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.UNAUTHORIZED:
                await self.invalidate_session(target)
            return SaltServerVersion(version=None, raw=None, error=f"HTTPStatusError: {exc}")
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            return SaltServerVersion(version=None, raw=None, error=f"{type(exc).__name__}: {exc}")
        return parse_server_version(ret)

    async def fingerprint(
        self,
        target: SaltTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from :meth:`server_version`."""
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        read = await self.server_version(target, eff_operator)
        probe_method = f"POST {RPC_PATH} api.get_versions"
        if read.error is not None:
            return FingerprintResult(
                vendor="vmware",
                product="salt",
                reachable=False,
                probed_at=probed_at,
                probe_method=probe_method,
                extras={"error": read.error},
            )
        return FingerprintResult(
            vendor="vmware",
            product="salt",
            version=read.version,
            build=read.raw,
            reachable=True,
            probed_at=probed_at,
            probe_method=probe_method,
        )

    async def probe(self, target: SaltTargetLike) -> ProbeResult:
        """Reachability + login check; delegates to :meth:`fingerprint`."""
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=fp.probed_at)
        return ProbeResult(
            ok=False,
            reason=str(fp.extras.get("error", "unreachable")),
            probed_at=fp.probed_at,
        )

    async def execute(
        self,
        target: SaltTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the dispatcher (same shape as the srm connector)."""
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:salt-raas-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        return await dispatch(
            operator=operator,
            connector_id=f"{self.impl_id}-{self.version}",
            op_id=op_id,
            target=target,
            params=params,
        )

    # Typed-op handlers. The bodies live in :mod:`.reads`; the dispatcher
    # resolves ``module.ClassName.method`` and passes operator, target, params.

    async def about(
        self, operator: Operator, target: SaltTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``salt.about`` -- versions and masters."""
        from meho_backplane.connectors.salt.reads import about_impl

        return await about_impl(self, operator, target, params)

    async def minion_list(
        self, operator: Operator, target: SaltTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``salt.minion.list`` -- minions with key state, presence and OS."""
        from meho_backplane.connectors.salt.reads import minion_list_impl

        return await minion_list_impl(self, operator, target, params)

    async def key_list(
        self, operator: Operator, target: SaltTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``salt.key.list`` -- minion keys and their states."""
        from meho_backplane.connectors.salt.reads import key_list_impl

        return await key_list_impl(self, operator, target, params)

    async def job_list(
        self, operator: Operator, target: SaltTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``salt.job.list`` -- recent job runs."""
        from meho_backplane.connectors.salt.reads import job_list_impl

        return await job_list_impl(self, operator, target, params)

    async def job_result(
        self, operator: Operator, target: SaltTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``salt.job.result`` -- the results of one job, per minion."""
        from meho_backplane.connectors.salt.reads import job_result_impl

        return await job_result_impl(self, operator, target, params)

    async def target_list(
        self, operator: Operator, target: SaltTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``salt.target.list`` -- saved targets (minion groups)."""
        from meho_backplane.connectors.salt.reads import target_list_impl

        return await target_list_impl(self, operator, target, params)

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Filter a vendor answer before the flight recorder records it.

        The shared redaction knows secrets by shape and by op family. A
        pillar value in a job return, or a password a job read from a file,
        is neither, so every ``/rpc`` answer passes the rules of the read
        that made the call (:func:`~meho_backplane.connectors.salt.trace.trace_payload`)
        before the trace stores it. A body that is not JSON is recorded as
        the shared engine sees fit.
        """
        from meho_backplane.connectors.salt.trace import trace_payload

        try:
            request = json.loads(response.request.content)
            payload = response.json()
        except (RuntimeError, ValueError):
            return None
        return trace_payload(request, payload)

    async def aclose(self) -> None:
        """Clear cached sessions, then tear down the httpx pool."""
        async with self._session_lock:
            self._sessions.clear()
        await super().aclose()
