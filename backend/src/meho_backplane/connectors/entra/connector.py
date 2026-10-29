# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""EntraConnector: typed reads of Microsoft Entra ID through Microsoft Graph.

One target is one Entra ID tenant. The connector sits on the shared HTTP
base, so every call keeps MEHO's pooled client, its blocking of private
addresses and its trace recording. Its calls go through
:func:`meho_backplane.connectors.entra._http.send`, which owns the one retry
budget per call and the wait when Graph throttles. The retry of the HTTP base
is not used.

The target's secret holds a Microsoft Entra ID login (tenant ID, client ID and
client secret), the same shape as an Azure target's secret. MEHO trades it for
an access token for Microsoft Graph
(:mod:`meho_backplane.connectors._shared.microsoft_login`, scope
``https://graph.microsoft.com/.default``) and sends the token as
``Authorization: Bearer``.

Secrets and tokens are cached here, like in the Azure connector: the secret
per MEHO tenant, target and secret reference; the token per MEHO tenant,
target, Entra tenant, client ID and scope. MEHO asks for a new token five
minutes before the old one runs out. One lock per cache key makes many calls
at the same moment start one login. A call without a person's token never
gets a cached secret or token. The Graph token lives in this connector's own
cache, apart from the Azure token of the same login.

After Graph answers 401 or 403, :func:`~meho_backplane.connectors.entra._http.send`
calls :meth:`EntraConnector.invalidate_session`. The dispatcher calls
:meth:`EntraConnector.invalidate_credentials` after Microsoft refused the
login. ``docs/codebase/connectors-entra.md`` describes the whole flow.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors._shared.microsoft_login import (
    MicrosoftAccessToken,
    MicrosoftLoginError,
    fetch_client_credentials_token,
)
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    is_acceptable_auth_model,
)
from meho_backplane.connectors.adapters.http import HttpConnector, json_payload_or_empty
from meho_backplane.connectors.entra._credentials import scrub_credentials
from meho_backplane.connectors.entra._errors import EntraApiError, EntraUrlRefusedError
from meho_backplane.connectors.entra._http import NoRedirectClient, send
from meho_backplane.connectors.entra._paths import ORGANIZATION_PATH, ORGANIZATION_PERMISSION
from meho_backplane.connectors.entra.session import (
    EntraCredentials,
    EntraCredentialsLoader,
    EntraTargetConfig,
    EntraTargetConfigError,
    EntraTargetLike,
    check_tenant_match,
    load_credentials_from_vault,
    resolve_target_config,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["EntraConnector"]

_log = structlog.get_logger(__name__)

#: Ask for a new token this many seconds before the old one runs out, the same
#: margin as the Azure connector and Microsoft's own SDK.
_RENEW_BEFORE_EXPIRY_SECONDS: Final[float] = 300.0

#: The probe's description, stored with the fingerprint.
_PROBE_METHOD: Final[str] = "GET /v1.0/organization (Microsoft Graph)"

#: Errors that mean "the target cannot be read now". The fingerprint turns them
#: into ``reachable=False`` with the reason, instead of failing the probe.
_UNREACHABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    httpx.HTTPError,
    OSError,
    VaultClientError,
    CredentialsReadError,
    EntraTargetConfigError,
    MicrosoftLoginError,
    ConnectorAuthError,
    EntraApiError,
    EntraUrlRefusedError,
)

#: What a trace records when MEHO could not filter a response body.
_BODY_WITHHELD: Final[str] = "<response body left out: MEHO could not filter it>"

#: A clock for the cache. Tests replace it to move time forward.
_monotonic = time.monotonic

_CredentialKey = tuple[str, str, str]
_TokenKey = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class _CachedToken:
    """A token and the clock time at which MEHO asks for a new one."""

    token: MicrosoftAccessToken
    renew_at: float


def _may_use_cache(operator: Operator) -> bool:
    """Only a call with a person's token may use a cached secret or token."""
    return bool(operator.raw_jwt) and not is_system_operator(operator)


class EntraConnector(HttpConnector):
    """Microsoft Entra ID connector. One target is one Entra ID tenant.

    Registered as ``("entra", "1.0", "entra-graph")``, so the connector ID is
    ``entra-graph-1.0``, plus the ``("entra", "", "")`` row that lets every
    ``entra`` target find it. The version is the Microsoft Graph API version;
    Entra ID has no product version, so ``supported_version_range`` stays
    ``None``.
    """

    product = "entra"
    version = "1.0"
    impl_id = "entra-graph"
    supported_version_range: str | None = None
    priority = 1
    #: No redirect is followed: every tenant shares one Graph host.
    _client_class = NoRedirectClient

    def __init__(self, *, credentials_loader: EntraCredentialsLoader | None = None) -> None:
        super().__init__()
        self._credentials_loader: EntraCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._credentials: dict[_CredentialKey, EntraCredentials] = {}
        self._tokens: dict[_TokenKey, _CachedToken] = {}
        self._credential_locks: dict[_CredentialKey, asyncio.Lock] = {}
        self._token_locks: dict[_TokenKey, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Login and cache
    # ------------------------------------------------------------------

    async def auth_headers(self, target: EntraTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}`` for a call to *target*."""
        headers, _token = await self.authorize(target, operator)
        return headers

    async def authorize(
        self, target: EntraTargetLike, operator: Operator
    ) -> tuple[dict[str, str], MicrosoftAccessToken]:
        """Return the ``Authorization`` header and the token it carries."""
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"EntraConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target {target.name!r} "
                f"asked for auth_model={auth_model!r}"
            )
        token = await self._access_token(target, operator)
        return {"Authorization": f"Bearer {token.value}"}, token

    async def _access_token(
        self, target: EntraTargetLike, operator: Operator
    ) -> MicrosoftAccessToken:
        """Return a Microsoft Graph token for *target*, from the cache or a new login."""
        config = resolve_target_config(target)
        credentials = await self._credentials_for(target, operator)
        check_tenant_match(target, config, credentials)
        scope = config.cloud.graph_scope
        key: _TokenKey = (
            *target_cache_key(target),
            credentials.tenant_id,
            credentials.client_id,
            scope,
        )
        use_cache = _may_use_cache(operator)
        cached = self._tokens.get(key)
        if use_cache and cached is not None and _monotonic() < cached.renew_at:
            return cached.token
        async with self._token_locks.setdefault(key, asyncio.Lock()):
            cached = self._tokens.get(key)
            if use_cache and cached is not None and _monotonic() < cached.renew_at:
                return cached.token
            token = await fetch_client_credentials_token(
                credentials,
                login_host=config.cloud.login_host,
                scope=scope,
                target_name=target.name,
                secret_ref=target.secret_ref,
            )
            lifetime = max(1.0, token.expires_in - _RENEW_BEFORE_EXPIRY_SECONDS)
            self._tokens[key] = _CachedToken(token=token, renew_at=_monotonic() + lifetime)
            _log.info(
                "entra_login_done",
                target=target.name,
                cloud=config.cloud.name,
                entra_tenant_id=credentials.tenant_id,
                client_id=credentials.client_id,
                expires_in=token.expires_in,
            )
            return token

    async def _credentials_for(
        self, target: EntraTargetLike, operator: Operator
    ) -> EntraCredentials:
        """Return the target's login, from the cache or from its secret."""
        key: _CredentialKey = (*target_cache_key(target), str(target.secret_ref or ""))
        use_cache = _may_use_cache(operator)
        cached = self._credentials.get(key)
        if use_cache and cached is not None:
            return cached
        async with self._credential_locks.setdefault(key, asyncio.Lock()):
            cached = self._credentials.get(key)
            if use_cache and cached is not None:
                return cached
            credentials = await self._credentials_loader(target, operator)
            self._credentials[key] = credentials
            return credentials

    async def invalidate_session(self, target: EntraTargetLike) -> None:
        """Drop the target's cached tokens (after a 401 or a 403 from Graph).

        The next call logs in again with the cached secret. The secret stays:
        a 401 or a 403 means the token no longer fits, not that the secret is
        wrong.
        """
        self._drop_tokens(target)

    async def invalidate_credentials(self, target: EntraTargetLike) -> None:
        """Drop the target's cached secret and tokens (the failed-login hook).

        The dispatcher calls this when Microsoft refuses the login. The next
        call reads the secret again, so a corrected secret works without a
        restart.
        """
        prefix = target_cache_key(target)
        for key in [key for key in self._credentials if key[:2] == prefix]:
            del self._credentials[key]
        self._drop_tokens(target)

    def _drop_tokens(self, target: EntraTargetLike) -> None:
        prefix = target_cache_key(target)
        for key in [key for key in self._tokens if key[:2] == prefix]:
            del self._tokens[key]

    async def aclose(self) -> None:
        """Forget every cached secret and token, then close the HTTP clients."""
        self._credentials.clear()
        self._tokens.clear()
        await super().aclose()

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Record Graph answers with every app credential cut down to its safe fields.

        An app list carries ``hint`` (the first characters of each client
        secret) and certificate data. Neither may reach a stored trace. A body
        that is not JSON holds no credentials and is recorded as it is. Never
        raises.
        """
        try:
            body = response.json()
        except ValueError:
            return None
        try:
            return scrub_credentials(body)
        except Exception:  # a trace must never break the call, and never leak
            return _BODY_WITHHELD

    # ------------------------------------------------------------------
    # Microsoft Graph reads
    # ------------------------------------------------------------------

    async def graph_get(
        self,
        target: EntraTargetLike,
        path: str,
        *,
        operator: Operator,
        permission: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET *path* on the target's Graph host and return the JSON object."""
        response = await send(
            self, target, path, operator=operator, permission=permission, params=params
        )
        body: Any = json_payload_or_empty(response)
        return body if isinstance(body, dict) else {}

    async def read_organization(
        self, target: EntraTargetLike, operator: Operator
    ) -> tuple[EntraTargetConfig, dict[str, Any]]:
        """Read the target's tenant. Returns the checked settings and Graph's object."""
        config = resolve_target_config(target)
        answer = await self.graph_get(
            target,
            ORGANIZATION_PATH,
            operator=operator,
            permission=ORGANIZATION_PERMISSION,
            params={"$select": "id,displayName,verifiedDomains"},
        )
        value = answer.get("value")
        first = value[0] if isinstance(value, list) and value else {}
        return config, first if isinstance(first, dict) else {}

    async def login_identity(
        self, target: EntraTargetLike, operator: Operator
    ) -> MicrosoftAccessToken:
        """Return the token the next call to *target* uses, to show its ids."""
        return await self._access_token(target, operator)

    # ------------------------------------------------------------------
    # Fingerprint and probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: EntraTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read the tenant and describe the target.

        ``vendor="microsoft"``, ``product="entra"``. Entra ID has no product
        version, so ``version`` stays ``None``. ``extras`` holds the tenant
        ID, the cloud and the default domain. A call without an operator runs
        as MEHO's system identity, which cannot read the secret, so it reports
        ``reachable=False`` with the reason.
        """
        probed_at = datetime.now(UTC)
        effective_operator = operator if operator is not None else synthesise_system_operator()
        try:
            config, organization = await self.read_organization(target, effective_operator)
        except _UNREACHABLE_ERRORS as exc:
            return FingerprintResult(
                vendor="microsoft",
                product="entra",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        from meho_backplane.connectors.entra.typed_reads import default_domain

        return FingerprintResult(
            vendor="microsoft",
            product="entra",
            version=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_PROBE_METHOD,
            extras={
                "tenant_id": organization.get("id"),
                "cloud": config.cloud.name,
                "default_domain": default_domain(organization),
            },
        )

    async def probe(self, target: EntraTargetLike) -> ProbeResult:
        """Check the target with the same tenant read as :meth:`fingerprint`."""
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
        target: EntraTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Old entry point. Sends the call through the dispatcher.

        Callers today use :func:`meho_backplane.operations.dispatch` directly.
        This path runs as MEHO's system identity, which cannot read the
        target's secret, so an Entra ID call through it fails on purpose.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:entra-graph-connector-shim",
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
    # Typed operations (bodies in typed_reads.py)
    # ------------------------------------------------------------------

    async def about(
        self, operator: Operator, target: EntraTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``entra.about``: the tenant of this target and the login MEHO uses."""
        from meho_backplane.connectors.entra.typed_reads import entra_about_impl

        return await entra_about_impl(self, operator, target, params)

    async def user_list(
        self, operator: Operator, target: EntraTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``entra.user.list``: the users of the tenant."""
        from meho_backplane.connectors.entra.typed_reads import entra_user_list_impl

        return await entra_user_list_impl(self, operator, target, params)

    async def group_list(
        self, operator: Operator, target: EntraTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``entra.group.list``: the groups of the tenant."""
        from meho_backplane.connectors.entra.typed_reads import entra_group_list_impl

        return await entra_group_list_impl(self, operator, target, params)

    async def role_assignment_list(
        self, operator: Operator, target: EntraTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``entra.role.assignment.list``: who holds which Entra ID admin role."""
        from meho_backplane.connectors.entra.typed_reads import entra_role_assignment_list_impl

        return await entra_role_assignment_list_impl(self, operator, target, params)

    async def app_list(
        self, operator: Operator, target: EntraTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``entra.app.list``: app registrations and the end dates of their credentials."""
        from meho_backplane.connectors.entra.typed_reads import entra_app_list_impl

        return await entra_app_list_impl(self, operator, target, params)

    async def service_principal_list(
        self, operator: Operator, target: EntraTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``entra.service_principal.list``: enterprise apps and their credential end dates."""
        from meho_backplane.connectors.entra.typed_reads import (
            entra_service_principal_list_impl,
        )

        return await entra_service_principal_list_impl(self, operator, target, params)
