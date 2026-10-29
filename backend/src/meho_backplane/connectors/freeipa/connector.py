# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FreeipaConnector: typed reads of Red Hat Identity Management (FreeIPA).

One target is one IPA server. FreeIPA has no OpenAPI description; its API is
JSON-RPC (:mod:`meho_backplane.connectors.freeipa.rpc`).

Login
-----

1. ``POST /ipa/session/login_password`` with the form fields ``user`` and
   ``password`` (an IPA user, read from Vault through the
   :class:`~meho_backplane.connectors.freeipa.session.FreeipaCredentialsLoader`).
   The answer sets the session cookies. A refused login answers ``401`` and
   names the reason in the ``X-IPA-Rejection-Reason`` header, for example
   ``invalid-password`` or ``password-expired``.
2. Every call is ``POST /ipa/session/json`` with the cookies.
3. Every request, the login too, sends ``Referer: https://<server>/ipa``.
   FreeIPA refuses a request whose ``Referer`` does not name the server's
   own host name with ``400``. The name is ``tls_server_name`` when the
   target sets it, else ``host``.
4. The session is cached per credential (``credential_cache_key``: tenant,
   secret and login host). A call that runs as MEHO's system identity never
   reads a cached session.
5. An expired session answers ``401``. The dispatcher then calls
   :meth:`FreeipaConnector.invalidate_session` with the rejected request and
   runs the call once more; the next call logs in again.

The read bodies live in :mod:`meho_backplane.connectors.freeipa.reads`.
``docs/codebase/connectors-freeipa.md`` describes the whole flow.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.cache_key import credential_cache_key
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors._shared.rejected_token import drop_rejected_token
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vcf_auth import (
    is_acceptable_auth_model,
    session_establish_auth_error,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.freeipa.rpc import (
    JSON_PATH,
    LOGIN_PATH,
    PING_COMMAND,
    FreeipaAnswer,
    build_request,
    parse_ping_summary,
    unwrap_answer,
)
from meho_backplane.connectors.freeipa.session import (
    FreeipaCredentialsLoader,
    FreeipaTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["FreeipaConnector", "FreeipaRequestRefusedError"]

_log = structlog.get_logger(__name__)

#: The probe's description, stored with the fingerprint.
_PROBE_METHOD: Final[str] = f"POST {JSON_PATH} {PING_COMMAND}"

#: The header in which FreeIPA names why it refused a login.
_REJECTION_HEADER: Final[str] = "X-IPA-Rejection-Reason"

#: Errors that mean "the target cannot be read now". The fingerprint turns them
#: into ``reachable=False`` with the reason, instead of failing the probe.
#: ``RuntimeError`` covers a refused login, a command error and a ``400``.
_UNREACHABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    httpx.HTTPError,
    OSError,
    ValueError,
    RuntimeError,
    VaultClientError,
    CredentialsReadError,
)

#: What every refused request with ``400`` most likely means.
_REFERER_HINT: Final[str] = (
    "FreeIPA refuses a request whose Referer header does not name the IPA "
    "server's own host name. When the target's host is an IP address or an "
    "alias, set tls_server_name to the server's host name."
)


class FreeipaRequestRefusedError(RuntimeError):
    """FreeIPA answered ``400``: most likely the ``Referer`` named the wrong host."""


@dataclass(frozen=True)
class _FreeipaSession:
    """One logged-in IPA session: what every call sends."""

    cookie: str
    referer: str

    def headers(self) -> dict[str, str]:
        return {"Cookie": self.cookie, "Referer": self.referer}


def referer_for(target: FreeipaTargetLike) -> str:
    """The ``Referer`` FreeIPA accepts: ``https://<server name>/ipa``."""
    server_name = getattr(target, "tls_server_name", None) or target.host
    return f"https://{server_name}/ipa"


class FreeipaConnector(HttpConnector):
    """FreeIPA JSON-RPC connector: password login, session cookie, ``Referer``.

    Registered as ``("freeipa", "4.x", "freeipa-jsonrpc")``, so the connector
    ID is ``freeipa-jsonrpc-4.x``, plus the ``("freeipa", "", "")`` row that
    lets every ``freeipa`` target find it.
    """

    product = "freeipa"
    version = "4.x"
    impl_id = "freeipa-jsonrpc"
    supported_version_range = ">=4.9,<5.0"
    priority = 1

    def __init__(self, *, credentials_loader: FreeipaCredentialsLoader | None = None) -> None:
        super().__init__()
        self._sessions: dict[tuple[str, ...], _FreeipaSession] = {}
        self._session_lock = asyncio.Lock()
        self._credentials_loader: FreeipaCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    # ------------------------------------------------------------------
    # Login and session
    # ------------------------------------------------------------------

    async def auth_headers(self, target: FreeipaTargetLike, operator: Operator) -> dict[str, str]:
        """Return the session headers (cookies and ``Referer``), logging in on first use.

        Raises :exc:`NotImplementedError` when ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"FreeipaConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        session = await self._session(target, operator)
        return session.headers()

    async def _session(self, target: FreeipaTargetLike, operator: Operator) -> _FreeipaSession:
        """Return the cached session for *target*, logging in on first use."""
        cache_key = credential_cache_key(target)
        async with self._session_lock:
            cached = self._sessions.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            creds = await self._credentials_loader(target, operator)
            try:
                username, password = creds["username"], creds["password"]
            except KeyError as exc:
                raise RuntimeError(
                    f"freeipa credentials loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            session = await self._login(target, username, password)
            self._sessions[cache_key] = session
            return session

    async def _login(
        self, target: FreeipaTargetLike, username: str, password: str
    ) -> _FreeipaSession:
        """Log in with a password and return the session. The login is never traced."""
        referer = referer_for(target)
        client = await self._http_client(target)
        resp = await client.post(
            LOGIN_PATH,
            data={"user": username, "password": password},
            headers={"Referer": referer, "Accept": "text/plain"},
            extensions=self._request_extensions(target),
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            reason = resp.headers.get(_REJECTION_HEADER)
            message = (
                f"freeipa login failed for target {target.name!r}: POST {LOGIN_PATH} "
                f"returned HTTP {resp.status_code}" + (f" ({reason})" if reason else "")
            )
            if resp.status_code == httpx.codes.BAD_REQUEST:
                raise FreeipaRequestRefusedError(f"{message}. {_REFERER_HINT}") from exc
            raise (
                session_establish_auth_error(exc, message=message, target=target)
                or RuntimeError(message)
            ) from exc
        cookies = {cookie.name: cookie.value for cookie in resp.cookies.jar if cookie.value}
        if not cookies:
            raise RuntimeError(
                f"freeipa login for target {target.name!r} answered without a session cookie"
            )
        _log.info(
            "freeipa_session_established",
            target=target.name,
            host=target.host,
            cookie_names=sorted(cookies),
        )
        return _FreeipaSession(
            cookie="; ".join(f"{name}={value}" for name, value in cookies.items()),
            referer=referer,
        )

    async def invalidate_session(
        self, target: FreeipaTargetLike, *, rejected: httpx.Request | None = None
    ) -> None:
        """Drop the cached session of *target*'s credential (the dispatcher's 401 hook).

        With *rejected*, the session is dropped only when that request sent
        it; a newer session stays. When it is dropped, the pooled client's
        cookie jar for this target is cleared too, and the next call logs in
        again with credentials read again through the loader.
        """
        cache_key = credential_cache_key(target)
        async with self._session_lock:
            dropped = drop_rejected_token(
                self._sessions, cache_key, rejected, lambda session: session.headers()
            )
            client = self._clients.get(self._client_cache_key(target)) if dropped else None
            if client is not None:
                client.cookies.clear()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def call(
        self,
        target: FreeipaTargetLike,
        operator: Operator,
        command: str,
        args: list[Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> FreeipaAnswer:
        """Run one IPA command and return its decoded answer.

        A command error raises
        :class:`~meho_backplane.connectors.freeipa.rpc.FreeipaCommandError`.
        A ``400`` raises :class:`FreeipaRequestRefusedError`; every other
        HTTP error (a ``401`` included) reaches the dispatcher as it is.
        """
        try:
            payload = await self._post_json(
                target,
                JSON_PATH,
                operator=operator,
                json=build_request(command, args, options),
                extra_headers={"Accept": "application/json"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.BAD_REQUEST:
                raise FreeipaRequestRefusedError(
                    f"freeipa {command} on target {target.name!r} returned HTTP 400. "
                    f"{_REFERER_HINT}"
                ) from exc
            raise
        return unwrap_answer(payload, command)

    # ------------------------------------------------------------------
    # Fingerprint and probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: FreeipaTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Run ``ping`` and describe the target.

        ``vendor`` and ``product`` are ``freeipa``. ``version`` is the server
        version; ``extras`` holds the API version and the IPA principal MEHO
        logged in as. A call without an operator runs as MEHO's system
        identity, which cannot read the secret, so it reports
        ``reachable=False`` with the reason.
        """
        probed_at = datetime.now(UTC)
        effective_operator = operator if operator is not None else synthesise_system_operator()
        try:
            answer = await self.call(target, effective_operator, PING_COMMAND)
        except _UNREACHABLE_ERRORS as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == httpx.codes.UNAUTHORIZED
            ):
                await self.invalidate_session(target, rejected=exc.request)
            return FingerprintResult(
                vendor="freeipa",
                product="freeipa",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        summary = answer.result.get("summary") if isinstance(answer.result, dict) else None
        server_version, api_version = parse_ping_summary(summary)
        return FingerprintResult(
            vendor="freeipa",
            product="freeipa",
            version=server_version or answer.server_version,
            reachable=True,
            probed_at=probed_at,
            probe_method=_PROBE_METHOD,
            extras={"api_version": api_version, "principal": answer.principal},
        )

    async def probe(self, target: FreeipaTargetLike) -> ProbeResult:
        """Check the target with the same ``ping`` as :meth:`fingerprint`."""
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
        target: FreeipaTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Old entry point. Sends the call through the dispatcher.

        This path runs as MEHO's system identity, which cannot read the
        target's secret, so a FreeIPA call through it fails on purpose.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:freeipa-jsonrpc-connector-shim",
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

    # ------------------------------------------------------------------
    # Typed operations (bodies in reads.py). The dispatcher resolves
    # ``module.ClassName.method`` and passes operator, target and params.
    # ------------------------------------------------------------------

    async def about(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.about``: server version, API version, MEHO's IPA login."""
        from meho_backplane.connectors.freeipa.reads import about_impl

        return await about_impl(self, operator, target, params)

    async def host_list(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.host.list``: hosts, also by host group."""
        from meho_backplane.connectors.freeipa.reads import host_list_impl

        return await host_list_impl(self, operator, target, params)

    async def host_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.host.show``: one host."""
        from meho_backplane.connectors.freeipa.reads import host_show_impl

        return await host_show_impl(self, operator, target, params)

    async def hostgroup_list(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hostgroup.list``: host groups."""
        from meho_backplane.connectors.freeipa.reads import hostgroup_list_impl

        return await hostgroup_list_impl(self, operator, target, params)

    async def hostgroup_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hostgroup.show``: one host group and its members."""
        from meho_backplane.connectors.freeipa.reads import hostgroup_show_impl

        return await hostgroup_show_impl(self, operator, target, params)

    async def user_list(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.user.list``: users."""
        from meho_backplane.connectors.freeipa.reads import user_list_impl

        return await user_list_impl(self, operator, target, params)

    async def user_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.user.show``: one user."""
        from meho_backplane.connectors.freeipa.reads import user_show_impl

        return await user_show_impl(self, operator, target, params)

    async def group_list(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.group.list``: user groups."""
        from meho_backplane.connectors.freeipa.reads import group_list_impl

        return await group_list_impl(self, operator, target, params)

    async def group_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.group.show``: one user group and its members."""
        from meho_backplane.connectors.freeipa.reads import group_show_impl

        return await group_show_impl(self, operator, target, params)

    async def role_list(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.role.list``: roles."""
        from meho_backplane.connectors.freeipa.reads import role_list_impl

        return await role_list_impl(self, operator, target, params)

    async def role_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.role.show``: one role, its members and privileges."""
        from meho_backplane.connectors.freeipa.reads import role_show_impl

        return await role_show_impl(self, operator, target, params)

    async def sudorule_list(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.sudorule.list``: sudo rules."""
        from meho_backplane.connectors.freeipa.reads import sudorule_list_impl

        return await sudorule_list_impl(self, operator, target, params)

    async def sudorule_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.sudorule.show``: one sudo rule."""
        from meho_backplane.connectors.freeipa.reads import sudorule_show_impl

        return await sudorule_show_impl(self, operator, target, params)

    async def hbacrule_list(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbacrule.list``: access rules (HBAC)."""
        from meho_backplane.connectors.freeipa.reads import hbacrule_list_impl

        return await hbacrule_list_impl(self, operator, target, params)

    async def hbacrule_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbacrule.show``: one access rule."""
        from meho_backplane.connectors.freeipa.reads import hbacrule_show_impl

        return await hbacrule_show_impl(self, operator, target, params)

    async def hbac_test(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbac.test``: may this user use this service on this host?"""
        from meho_backplane.connectors.freeipa.reads import hbac_test_impl

        return await hbac_test_impl(self, operator, target, params)

    async def pwpolicy_show(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.pwpolicy.show``: a group's password policy, or the global one."""
        from meho_backplane.connectors.freeipa.reads import pwpolicy_show_impl

        return await pwpolicy_show_impl(self, operator, target, params)

    async def aclose(self) -> None:
        """Forget every cached session, then close the HTTP clients."""
        async with self._session_lock:
            self._sessions.clear()
        await super().aclose()
