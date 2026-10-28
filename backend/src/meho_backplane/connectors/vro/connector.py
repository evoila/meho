# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VroConnector -- hand-written HttpConnector subclass for VMware Orchestrator.

Covers Orchestrator 8.x (on its own, or inside Aria Automation 8.x) and
the VCF Operations orchestrator of VCF 9, through the Orchestrator REST
API at ``/vco/api``.

Login
-----

Chosen per target with ``extras["login"]`` (see
:func:`meho_backplane.connectors.vro.session.login_mode`):

* ``basic`` (default): ``Authorization: Basic`` on every call.
* ``vcfa-session`` (VCF 9): a VCF Automation session token for
  ``<user>@<extras.org>`` from the shared login
  (:mod:`meho_backplane.connectors._shared.vcfa_session`), sent as
  ``Authorization: Bearer <token>``. The connector logs in again 60
  seconds before the token ends.
* ``automation``: the Aria Automation token, minted with the Aria
  Automation login (``POST /csp/gateway/am/api/login?access_token``, then
  ``POST /iaas/api/login``) on the same address; sent as
  ``Authorization: Bearer <token>``.

The ``Authorization`` value (and, for ``vcfa-session``, the time the
token ends) is cached per credential, login mode and org
(``credential_cache_key``: tenant, secret and login host). Only a caller
with an operator JWT, other than the synthesised system operator, reads or
fills the cache; any other caller runs the credential loader, which checks
it, and its login is not kept.
On ``401`` the dispatcher calls :meth:`invalidate_session` with the
rejected request. The cached value is dropped only when that request sent
it, so a late ``401`` never drops a newer login. The dispatcher then runs
the call once more.

Version
-------

``GET /vco/api/about`` returns ``version`` and ``build-number``.
:meth:`server_version` reads them and never raises; it backs
:meth:`fingerprint`. The operation ``vro.about`` returns the whole answer.

Operations
----------

Seven reads, bodies in :mod:`meho_backplane.connectors.vro.typed_reads`.
All exist on every covered version, so no operation needs a version check.

Traces
------

:meth:`trace_response_body` masks secret fields and secret workflow
parameter values before the flight recorder records a vendor answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import credential_cache_key
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors._shared.rejected_token import drop_rejected_token
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vcf_auth import basic_auth_header, is_acceptable_auth_model
from meho_backplane.connectors._shared.vcfa_session import (
    LOGIN_VCFA_SESSION,
    VcfaSessionToken,
    vcfa_session_login,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vro.session import (
    LOGIN_AUTOMATION,
    VroCredentialsLoader,
    VroTargetLike,
    load_credentials_from_vault,
    login_mode,
    vcfa_org,
)
from meho_backplane.connectors.vro.typed_reads import ABOUT_PATH, redact_secrets

__all__ = ["VroConnector", "VroServerVersion", "parse_server_version"]

_log = structlog.get_logger(__name__)

_JSON_ACCEPT = "application/json"


@dataclass(frozen=True)
class VroServerVersion:
    """The Orchestrator version as :meth:`VroConnector.server_version` reads it.

    ``error`` names why the read failed; then ``version`` and ``build``
    are ``None``.
    """

    version: str | None
    build: str | None
    error: str | None


def parse_server_version(payload: Any) -> VroServerVersion:
    """Pick ``version`` and ``build-number`` out of a ``/vco/api/about`` answer."""
    if not isinstance(payload, dict):
        return VroServerVersion(version=None, build=None, error="about is not a JSON object")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        return VroServerVersion(version=None, build=None, error="about has no version")
    build_raw = payload.get("build-number")
    build = str(build_raw) if build_raw not in (None, "") else None
    return VroServerVersion(version=version, build=build, error=None)


def _username_password(creds: dict[str, str], target: VroTargetLike) -> tuple[str, str]:
    """``(username, password)`` from *creds*; a ``RuntimeError`` names a missing key."""
    try:
        return creds["username"], creds["password"]
    except KeyError as exc:
        raise RuntimeError(
            f"vro credentials loader for target {target.name!r} returned a "
            f"dict missing required key {exc.args[0]!r}; need "
            "{'username': str, 'password': str}"
        ) from exc


class VroConnector(HttpConnector):
    """Orchestrator REST API connector (``/vco/api``) with three logins (see the module doc)."""

    product = "vro"
    version = "9.0"
    impl_id = "vro-rest"
    supported_version_range = ">=8.0,<10.0"
    priority = 1

    def __init__(self, *, credentials_loader: VroCredentialsLoader | None = None) -> None:
        super().__init__()
        #: Per credential: the ``Authorization`` value, and the session
        #: token when it ends (``vcfa-session``); ``None`` for the other logins.
        self._authorizations: dict[tuple[str, ...], tuple[str, VcfaSessionToken | None]] = {}
        self._auth_lock = asyncio.Lock()
        self._credentials_loader: VroCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def auth_headers(self, target: VroTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": ...}``, logging in on first use.

        Raises :exc:`NotImplementedError` when ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"VroConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        return {"Authorization": await self._authorization(target, operator)}

    async def _authorization(self, target: VroTargetLike, operator: Operator) -> str:
        """Return the cached ``Authorization`` value for *target*, logging in on first use.

        Only a caller with an operator JWT who is not the synthesised system
        operator reads or fills the cache. Any other caller runs the
        credential loader, which checks it (the Vault loader fails closed
        without a valid JWT), and its login is not kept: a value an
        authenticated caller primed is never served to a system or
        unauthenticated caller, and such a caller never primes it.
        """
        mode = login_mode(target)
        cache_key = credential_cache_key(target, mode, vcfa_org(target))
        checked = bool(operator.raw_jwt) and not is_system_operator(operator)
        async with self._auth_lock:
            cached = self._authorizations.get(cache_key)
            if cached is not None and checked:
                value, session = cached
                if session is None or not session.needs_renewal():
                    return value
            creds = await self._credentials_loader(target, operator)
            session = None
            if mode == LOGIN_AUTOMATION:
                from meho_backplane.connectors.vra8._auth import vra8_login

                client = await self._http_client(target)
                token = await vra8_login(
                    client, creds, target, request_extensions=self._request_extensions(target)
                )
                value = f"Bearer {token}"
            elif mode == LOGIN_VCFA_SESSION:
                username, password = _username_password(creds, target)
                session = await vcfa_session_login(
                    await self._http_client(target),
                    username=username,
                    password=password,
                    org=vcfa_org(target),
                    target=target,
                    request_extensions=self._request_extensions(target),
                )
                value = session.authorization
            else:
                value = basic_auth_header(*_username_password(creds, target))
            if checked:
                self._authorizations[cache_key] = (value, session)
            _log.info("vro_login_ready", target=target.name, host=target.host, login=mode)
            return value

    async def invalidate_session(
        self, target: VroTargetLike, *, rejected: httpx.Request | None = None
    ) -> None:
        """Drop the cached login of *target*'s credential (the dispatcher's 401 hook).

        With *rejected*, the login is dropped only when that request sent
        it; a newer login stays.
        """
        cache_key = credential_cache_key(target, login_mode(target), vcfa_org(target))
        async with self._auth_lock:
            drop_rejected_token(
                self._authorizations,
                cache_key,
                rejected,
                lambda cached: {"Authorization": cached[0]},
            )

    async def _vro_get(
        self,
        target: VroTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried GET returning parsed JSON; asks for JSON (the API answers XML by default)."""
        return await self._request_json(
            target,
            "GET",
            path,
            operator=operator,
            params=params,
            extra_headers={"Accept": _JSON_ACCEPT},
        )

    async def server_version(self, target: VroTargetLike, operator: Operator) -> VroServerVersion:
        """Read the Orchestrator version and build. Never raises: a failure is named in ``error``.

        A ``401`` drops the cached login, so the next call logs in again.
        """
        try:
            payload = await self._vro_get(target, ABOUT_PATH, operator=operator)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.UNAUTHORIZED:
                await self.invalidate_session(target, rejected=exc.request)
            return VroServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        except (
            httpx.HTTPError,
            OSError,
            RuntimeError,
            ValueError,
            CredentialsReadError,
        ) as exc:
            return VroServerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        return parse_server_version(payload)

    async def fingerprint(
        self,
        target: VroTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from :meth:`server_version`."""
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        read = await self.server_version(target, eff_operator)
        if read.error is not None:
            return FingerprintResult(
                vendor="vmware",
                product="vro",
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {ABOUT_PATH}",
                extras={"error": read.error},
            )
        return FingerprintResult(
            vendor="vmware",
            product="vro",
            version=read.version,
            build=read.build,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {ABOUT_PATH}",
        )

    async def probe(self, target: VroTargetLike) -> ProbeResult:
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
        target: VroTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the dispatcher (same shape as the srm connector)."""
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:vro-rest-connector-shim",
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
        self, operator: Operator, target: VroTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vro.about`` shim."""
        from meho_backplane.connectors.vro.typed_reads import vro_about_impl

        return await vro_about_impl(self, operator, target, params)

    async def health(
        self, operator: Operator, target: VroTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vro.health`` shim."""
        from meho_backplane.connectors.vro.typed_reads import vro_health_impl

        return await vro_health_impl(self, operator, target, params)

    async def workflow_list(
        self, operator: Operator, target: VroTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vro.workflow.list`` shim."""
        from meho_backplane.connectors.vro.typed_reads import vro_workflow_list_impl

        return await vro_workflow_list_impl(self, operator, target, params)

    async def workflow_get(
        self, operator: Operator, target: VroTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vro.workflow.get`` shim."""
        from meho_backplane.connectors.vro.typed_reads import vro_workflow_get_impl

        return await vro_workflow_get_impl(self, operator, target, params)

    async def execution_list(
        self, operator: Operator, target: VroTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vro.execution.list`` shim."""
        from meho_backplane.connectors.vro.typed_reads import vro_execution_list_impl

        return await vro_execution_list_impl(self, operator, target, params)

    async def execution_get(
        self, operator: Operator, target: VroTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vro.execution.get`` shim."""
        from meho_backplane.connectors.vro.typed_reads import vro_execution_get_impl

        return await vro_execution_get_impl(self, operator, target, params)

    async def execution_logs(
        self, operator: Operator, target: VroTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vro.execution.logs`` shim."""
        from meho_backplane.connectors.vro.typed_reads import vro_execution_logs_impl

        return await vro_execution_logs_impl(self, operator, target, params)

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Mask secret fields and secret parameter values before the trace records the answer."""
        try:
            payload = response.json()
        except ValueError:
            return None
        return redact_secrets(payload)

    async def aclose(self) -> None:
        """Clear cached logins, then tear down the httpx pool."""
        async with self._auth_lock:
            self._authorizations.clear()
        await super().aclose()
