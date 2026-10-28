# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared base for the Live Recovery REST API gateways (SRM and vSphere Replication).

Site Recovery Manager (``/api/rest/srm/v2``) and vSphere Replication
(``/api/rest/vr/v2``) serve the same REST API gateway. On VCF 9.1 one
Protection and Recovery appliance serves both. The two APIs share:

* the login: ``POST <api base>/session`` with HTTP Basic; the JSON answer
  carries ``session_id``;
* the session header ``x-dr-session`` on every later call;
* the server summary: id, name, status, version, build, the vCenter and
  the certificates (``/server/info`` for SRM, ``/info`` for vSphere
  Replication);
* the API description files the appliance serves.

:class:`DrRestConnector` holds this shared part. A subclass sets the
product, the version range and the paths: :attr:`DrRestConnector.api_base`,
:attr:`DrRestConnector.session_path`, :attr:`DrRestConnector.info_path` and
:attr:`DrRestConnector.api_files`. It also defines the two operation entry
methods, ``about`` and ``api_file_get``, which call the shared code here
(:meth:`DrRestConnector.server_about` and
:func:`~meho_backplane.connectors.vmware_shared.dr_api_file.dr_api_file_get`).
The entry methods are not on this base class on purpose: the stored
handler of an operation names the class that defines the method, and the
dispatcher binds it only to a connector of that class. So ``srm.about``
cannot run on a ``vr`` target, and ``vr.about`` cannot run on an ``srm``
target: the dispatcher stops before any HTTP request.

Sessions are cached per target and tenant (``target_cache_key``), so two
targets on one address (a 9.1 appliance registered once as ``srm`` and
once as ``vr``) never share a session. On ``401`` from a later call the
dispatcher calls :meth:`DrRestConnector.invalidate_session`, which drops
only this target's session, and dispatches once more.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors._shared.vcf_auth import (
    basic_auth_header,
    is_acceptable_auth_model,
    session_establish_auth_error,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.flight_recorder import capture as flight_recorder_capture

__all__ = [
    "SESSION_HEADER",
    "DrRestConnector",
    "DrServerVersion",
    "DrSessionLoader",
    "DrTargetLike",
    "parse_server_version",
    "summarise_server_info",
]

_log = structlog.get_logger(__name__)

#: Header that carries the session id on every authenticated call.
SESSION_HEADER = "x-dr-session"

#: Fields of the server summary that ``about`` returns as they are.
_INFO_SCALARS = (
    "id",
    "name",
    "url",
    "server_status",
    "version",
    "build_number",
    "vc_guid",
    "vc_name",
    "extension_key",
)

#: Fields of one certificate that ``about`` keeps. The PEM text, public key
#: and signature are left out: they are long and add nothing to "which
#: server is this and is its certificate still valid".
_CERT_FIELDS = ("subject_dn", "issuer_dn", "serial_number", "not_before", "not_after", "thumbprint")


@runtime_checkable
class DrTargetLike(Protocol):
    """Minimum target shape :class:`DrRestConnector` reads.

    The concrete ``Target`` model satisfies this Protocol unchanged.
    ``id`` / ``tenant_id`` form the tenant-unique session cache key, so two
    targets that share one address never share a session. ``secret_ref``
    is the Vault path the loader resolves; ``port`` is optional (the
    appliance listens on 443).
    """

    id: object
    tenant_id: object
    name: str
    host: str
    port: int | None
    secret_ref: str | None
    auth_model: str | None


DrSessionLoader = Callable[[DrTargetLike, Operator], Awaitable[dict[str, str]]]
"""Async callable resolving a (target, operator) pair to credentials.

Returns ``{"username": ..., "password": ...}``. The connector calls the
loader on every login: on first use against a target, and again after a
``401`` made it drop the cached session.
"""


@dataclass(frozen=True)
class DrServerVersion:
    """The server version as :meth:`DrRestConnector.server_version` reads it.

    ``error`` names why the read failed; then ``version`` and ``build``
    are ``None``.
    """

    version: str | None
    build: str | None
    error: str | None


def parse_server_version(payload: Any) -> DrServerVersion:
    """Pick ``version`` and ``build_number`` out of a server summary answer."""
    if not isinstance(payload, dict):
        return DrServerVersion(
            version=None, build=None, error="server summary is not a JSON object"
        )
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        return DrServerVersion(version=None, build=None, error="server summary has no version")
    build_raw = payload.get("build_number")
    build = str(build_raw) if build_raw not in (None, "") else None
    return DrServerVersion(version=version, build=build, error=None)


def _certificate_summary(cert: Any) -> dict[str, Any]:
    if not isinstance(cert, dict):
        return {}
    return {key: cert.get(key) for key in _CERT_FIELDS}


def summarise_server_info(payload: Any) -> dict[str, Any]:
    """Reduce a server summary answer to the fields ``about`` returns."""
    info = payload if isinstance(payload, dict) else {}
    summary: dict[str, Any] = {key: info.get(key) for key in _INFO_SCALARS}
    certificates = info.get("certificates") or []
    summary["certificates"] = [_certificate_summary(cert) for cert in certificates]
    warnings = info.get("certificate_warnings") or []
    summary["certificate_warnings"] = [
        {
            "server_guid": warning.get("server_guid"),
            "server_url": warning.get("server_url"),
            "certificate": _certificate_summary(warning.get("certificate")),
        }
        for warning in warnings
        if isinstance(warning, dict)
    ]
    return summary


class DrRestConnector(HttpConnector):
    """Live Recovery REST API gateway connector with session-id auth (``x-dr-session``).

    Not registered itself. A subclass sets ``product``, ``version``,
    ``impl_id``, ``supported_version_range``, :attr:`api_base`,
    :attr:`session_path`, :attr:`info_path` and :attr:`api_files`, and
    defines the operation entry methods ``about`` and ``api_file_get``
    (see the module docstring for why they are not defined here).
    """

    #: Base path of the REST API, for example ``/api/rest/srm/v2``.
    api_base: ClassVar[str] = ""
    #: Full path of the login, ``<api base>/session``.
    session_path: ClassVar[str] = ""
    #: Full path of the server summary read.
    info_path: ClassVar[str] = ""
    #: The API description files ``api_file_get`` can read, by input name.
    api_files: ClassVar[dict[str, str]] = {}

    def __init__(self, *, session_loader: DrSessionLoader | None = None) -> None:
        super().__init__()
        self._session_ids: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._session_loader: DrSessionLoader = (
            session_loader if session_loader is not None else load_basic_credentials
        )

    @property
    def connector_id(self) -> str:
        """The dispatcher's connector id, ``<impl_id>-<version>``."""
        return f"{self.impl_id}-{self.version}"

    async def auth_headers(self, target: DrTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"x-dr-session": <id>}``, logging in on first use.

        Raises :exc:`NotImplementedError` when ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"{type(self).__name__} only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        session_id = await self._session_id(target, operator)
        return {SESSION_HEADER: session_id}

    async def _session_id(self, target: DrTargetLike, operator: Operator) -> str:
        """Return the cached session id for *target*, logging in on first use."""
        cache_key = target_cache_key(target)
        async with self._session_lock:
            cached = self._session_ids.get(cache_key)
            if cached is not None:
                return cached
            session_id = await self._login(target, operator)
            self._session_ids[cache_key] = session_id
            _log.info(
                "dr_rest_session_established",
                product=self.product,
                target=target.name,
                host=target.host,
            )
            return session_id

    async def _login(self, target: DrTargetLike, operator: Operator) -> str:
        """``POST <api base>/session`` with HTTP Basic; return the new session id."""
        path = self.session_path
        creds = await self._session_loader(target, operator)
        try:
            authorization = basic_auth_header(creds["username"], creds["password"])
        except KeyError as exc:
            raise RuntimeError(
                f"{self.product} session loader for target {target.name!r} returned "
                f"a dict missing required key {exc.args[0]!r}; need "
                "{'username': str, 'password': str}"
            ) from exc
        client = await self._http_client(target)
        try:
            resp = await client.post(
                path,
                headers={"Authorization": authorization, "Accept": "application/json"},
                extensions=self._request_extensions(target),
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPStatusError as exc:
            message = (
                f"{self.product} session establish failed for target {target.name!r}: "
                f"POST {path} returned HTTP {exc.response.status_code}"
            )
            raise (
                session_establish_auth_error(exc, message=message, target=target)
                or RuntimeError(message)
            ) from exc
        except ValueError as exc:
            raise RuntimeError(
                f"{self.product} session establish for target {target.name!r}: "
                f"POST {path} returned a body that is not JSON"
            ) from exc
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(
                f"{self.product} session establish for target {target.name!r}: "
                f"POST {path} returned 2xx with no session_id"
            )
        return session_id

    async def invalidate_session(self, target: DrTargetLike) -> None:
        """Drop the cached session for *target* only (the dispatcher's 401 hook).

        The next :meth:`auth_headers` call logs in again. Credentials are
        not cached: every login reads them through the loader.
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            self._session_ids.pop(cache_key, None)

    async def server_version(self, target: DrTargetLike, operator: Operator) -> DrServerVersion:
        """Read the server version and build. Never raises: a failure is named in ``error``.

        One ``GET`` of :attr:`info_path` with no retry, so a failed version
        read never costs the caller its own answer for long. A ``401``
        drops this target's cached session, so the next call logs in again.
        """
        try:
            headers = await self.auth_headers(target, operator)
            client = await self._http_client(target)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            return DrServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        span = flight_recorder_capture.span_start()
        try:
            resp = await client.get(
                self.info_path,
                headers={"Accept": "application/json", **headers},
                extensions=self._request_extensions(target),
            )
        except httpx.RequestError as exc:
            flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
            return DrServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        flight_recorder_capture.record_vendor_call(
            span, method="GET", request_headers=headers, response=resp
        )
        if resp.status_code == httpx.codes.UNAUTHORIZED:
            await self.invalidate_session(target)
        try:
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return DrServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        return parse_server_version(payload)

    async def fingerprint(
        self,
        target: DrTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from :meth:`server_version`."""
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        read = await self.server_version(target, eff_operator)
        if read.error is not None:
            return FingerprintResult(
                vendor="vmware",
                product=self.product,
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {self.info_path}",
                extras={"error": read.error},
            )
        return FingerprintResult(
            vendor="vmware",
            product=self.product,
            version=read.version,
            build=read.build,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {self.info_path}",
        )

    async def probe(self, target: DrTargetLike) -> ProbeResult:
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
        target: DrTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the dispatcher (same shape as the hcx connector)."""
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub=f"system:{self.impl_id}-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        return await dispatch(
            operator=operator,
            connector_id=self.connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )

    async def server_about(self, operator: Operator, target: DrTargetLike) -> dict[str, Any]:
        """``GET`` the server summary, reduced. The body of each ``<product>.about``."""
        payload = await self._get_json(target, self.info_path, operator=operator)
        return summarise_server_info(payload)

    async def aclose(self) -> None:
        """Clear cached sessions, then tear down the httpx pool."""
        async with self._session_lock:
            self._session_ids.clear()
        await super().aclose()
