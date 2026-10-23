# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SrmConnector -- hand-written HttpConnector subclass for Site Recovery Manager.

Covers SRM 8.8, Live Site Recovery 9.0 and the recovery API of VCF 9.1
Protection and Recovery, all through the SRM REST API v2 at
``/api/rest/srm/v2``.

Login
-----

1. ``POST /api/rest/srm/v2/session`` with HTTP Basic: the vCenter single
   sign-on account of the SRM site, read from Vault through the
   :class:`~meho_backplane.connectors.srm.session.SrmSessionLoader`.
2. The JSON answer carries ``session_id``. It is cached per target and
   tenant (``target_cache_key``), so two targets on one address (a 9.1
   appliance registered once as ``srm`` and once as ``vr``) never share
   a session.
3. Every later call sends the id in the ``x-dr-session`` header.
4. On ``401`` from a later call the dispatcher calls
   :meth:`invalidate_session`, which drops only this target's session,
   and dispatches once more; the next call logs in again.

Version
-------

``GET /api/rest/srm/v2/server/info`` returns the server's ``version`` and
``build_number``. :meth:`server_version` reads them and never raises; it
backs :meth:`fingerprint` and the ``srm.api_file.get`` answer. A ``401``
on this read drops this target's cached session, as the dispatcher would
for an operation, so the next call logs in again instead of sending the
rejected session once more. The operation ``srm.about`` returns the whole
server summary.

Operations
----------

``srm.about`` (:meth:`about`) and ``srm.api_file.get`` (body in
:mod:`meho_backplane.connectors.srm.api_file`). Both exist on 8.8, 9.0
and 9.1, so no operation needs a version check yet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
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
from meho_backplane.connectors.srm.session import (
    SrmSessionLoader,
    SrmTargetLike,
    load_session_credentials_from_vault,
)
from meho_backplane.flight_recorder import capture as flight_recorder_capture

__all__ = ["SrmConnector", "SrmServerVersion", "parse_server_version", "summarise_server_info"]

_log = structlog.get_logger(__name__)

#: Base path of the SRM REST API v2. The vendor file lists its paths
#: relative to it (``/session``, ``/server/info``).
API_BASE = "/api/rest/srm/v2"

#: Login. ``POST`` with HTTP Basic; the JSON answer carries ``session_id``.
_SESSION_PATH = f"{API_BASE}/session"

#: Server summary: id, name, status, version, build, the paired vCenter.
_SERVER_INFO_PATH = f"{API_BASE}/server/info"

#: Header that carries the session id on every authenticated call.
_SESSION_HEADER = "x-dr-session"

#: Fields of ``server/info`` that :meth:`SrmConnector.about` returns as they are.
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

#: Fields of one certificate that :meth:`SrmConnector.about` keeps. The PEM
#: text, public key and signature are left out: they are long and add nothing
#: to "which server is this and is its certificate still valid".
_CERT_FIELDS = ("subject_dn", "issuer_dn", "serial_number", "not_before", "not_after", "thumbprint")


@dataclass(frozen=True)
class SrmServerVersion:
    """The SRM version as :meth:`SrmConnector.server_version` reads it.

    ``error`` names why the read failed; then ``version`` and ``build``
    are ``None``.
    """

    version: str | None
    build: str | None
    error: str | None


def parse_server_version(payload: Any) -> SrmServerVersion:
    """Pick ``version`` and ``build_number`` out of a ``server/info`` answer."""
    if not isinstance(payload, dict):
        return SrmServerVersion(version=None, build=None, error="server/info is not a JSON object")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        return SrmServerVersion(version=None, build=None, error="server/info has no version")
    build_raw = payload.get("build_number")
    build = str(build_raw) if build_raw not in (None, "") else None
    return SrmServerVersion(version=version, build=build, error=None)


def _certificate_summary(cert: Any) -> dict[str, Any]:
    if not isinstance(cert, dict):
        return {}
    return {key: cert.get(key) for key in _CERT_FIELDS}


def summarise_server_info(payload: Any) -> dict[str, Any]:
    """Reduce a ``server/info`` answer to the fields ``srm.about`` returns."""
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


class SrmConnector(HttpConnector):
    """SRM REST API v2 connector with session-id auth (``x-dr-session``)."""

    product = "srm"
    version = "9.1"
    impl_id = "srm-rest"
    supported_version_range = ">=8.8,<10.0"
    priority = 1

    def __init__(self, *, session_loader: SrmSessionLoader | None = None) -> None:
        super().__init__()
        self._session_ids: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._session_loader: SrmSessionLoader = (
            session_loader if session_loader is not None else load_session_credentials_from_vault
        )

    async def auth_headers(self, target: SrmTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"x-dr-session": <id>}``, logging in on first use.

        Raises :exc:`NotImplementedError` when ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"SrmConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        session_id = await self._session_id(target, operator)
        return {_SESSION_HEADER: session_id}

    async def _session_id(self, target: SrmTargetLike, operator: Operator) -> str:
        """Return the cached session id for *target*, logging in on first use."""
        cache_key = target_cache_key(target)
        async with self._session_lock:
            cached = self._session_ids.get(cache_key)
            if cached is not None:
                return cached
            creds = await self._session_loader(target, operator)
            try:
                authorization = basic_auth_header(creds["username"], creds["password"])
            except KeyError as exc:
                raise RuntimeError(
                    f"srm session loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            client = await self._http_client(target)
            try:
                resp = await client.post(
                    _SESSION_PATH,
                    headers={"Authorization": authorization, "Accept": "application/json"},
                    extensions=self._request_extensions(target),
                )
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPStatusError as exc:
                message = (
                    f"srm session establish failed for target {target.name!r}: "
                    f"POST {_SESSION_PATH} returned HTTP {exc.response.status_code}"
                )
                raise (
                    session_establish_auth_error(exc, message=message, target=target)
                    or RuntimeError(message)
                ) from exc
            except ValueError as exc:
                raise RuntimeError(
                    f"srm session establish for target {target.name!r}: "
                    f"POST {_SESSION_PATH} returned a body that is not JSON"
                ) from exc
            session_id = payload.get("session_id") if isinstance(payload, dict) else None
            if not isinstance(session_id, str) or not session_id:
                raise RuntimeError(
                    f"srm session establish for target {target.name!r}: "
                    f"POST {_SESSION_PATH} returned 2xx with no session_id"
                )
            self._session_ids[cache_key] = session_id
            _log.info("srm_session_established", target=target.name, host=target.host)
            return session_id

    async def invalidate_session(self, target: SrmTargetLike) -> None:
        """Drop the cached session for *target* only (the dispatcher's 401 hook).

        The next :meth:`auth_headers` call logs in again. Credentials are
        not cached: every login reads them through the loader.
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            self._session_ids.pop(cache_key, None)

    async def server_version(self, target: SrmTargetLike, operator: Operator) -> SrmServerVersion:
        """Read the SRM version and build. Never raises: a failure is named in ``error``.

        One ``GET`` with no retry, so a failed version read never costs the
        caller its own answer for long. A ``401`` drops this target's
        cached session, so the next call logs in again.
        """
        try:
            headers = await self.auth_headers(target, operator)
            client = await self._http_client(target)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            return SrmServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        span = flight_recorder_capture.span_start()
        try:
            resp = await client.get(
                _SERVER_INFO_PATH,
                headers={"Accept": "application/json", **headers},
                extensions=self._request_extensions(target),
            )
        except httpx.RequestError as exc:
            flight_recorder_capture.record_vendor_call_error(span, method="GET", exc=exc)
            return SrmServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        flight_recorder_capture.record_vendor_call(
            span, method="GET", request_headers=headers, response=resp
        )
        if resp.status_code == httpx.codes.UNAUTHORIZED:
            await self.invalidate_session(target)
        try:
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SrmServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        return parse_server_version(payload)

    async def fingerprint(
        self,
        target: SrmTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from :meth:`server_version`."""
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        read = await self.server_version(target, eff_operator)
        if read.error is not None:
            return FingerprintResult(
                vendor="vmware",
                product="srm",
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {_SERVER_INFO_PATH}",
                extras={"error": read.error},
            )
        return FingerprintResult(
            vendor="vmware",
            product="srm",
            version=read.version,
            build=read.build,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {_SERVER_INFO_PATH}",
        )

    async def probe(self, target: SrmTargetLike) -> ProbeResult:
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
        target: SrmTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the dispatcher (same shape as the hcx connector)."""
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:srm-rest-connector-shim",
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

    async def about(
        self, operator: Operator, target: SrmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.about`` -- ``GET /api/rest/srm/v2/server/info``, reduced."""
        del params  # the schema declares no parameters
        payload = await self._get_json(target, _SERVER_INFO_PATH, operator=operator)
        return summarise_server_info(payload)

    async def api_file_get(
        self, operator: Operator, target: SrmTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.api_file.get`` shim; the body lives in :mod:`.api_file`."""
        from meho_backplane.connectors.srm.api_file import srm_api_file_get_impl

        return await srm_api_file_get_impl(self, operator, target, params)

    async def aclose(self) -> None:
        """Clear cached sessions, then tear down the httpx pool."""
        async with self._session_lock:
            self._session_ids.clear()
        await super().aclose()
