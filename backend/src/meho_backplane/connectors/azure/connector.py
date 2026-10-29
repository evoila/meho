# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""AzureConnector: typed reads of Azure Resource Manager, one subscription per target.

The connector sits on the shared HTTP base, so every call keeps MEHO's pooled
client, its blocking of private addresses and its trace recording. Its calls
go through :func:`meho_backplane.connectors.azure._http.send`, which owns the
one retry budget per call, the wait when Azure throttles and the limit per
Azure login. The retry of the HTTP base is not used.

The target's secret holds a Microsoft Entra ID login (tenant ID, client ID and
client secret). MEHO trades it for an access token
(:mod:`meho_backplane.connectors._shared.microsoft_login`) and sends the token
as ``Authorization: Bearer``. Secrets and tokens are cached here, one lock per
cache key, so many calls at the same moment start one login. A call without a
person's token never gets a cached secret or token.

After Azure answers 401, :func:`~meho_backplane.connectors.azure._http.send`
calls :meth:`AzureConnector.invalidate_session` and tries once more, inside
the same retry budget. The dispatcher calls
:meth:`AzureConnector.invalidate_credentials` after Microsoft refused the
login. ``docs/codebase/connectors-azure.md`` describes the whole flow.
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
from meho_backplane.connectors.azure._errors import AzureApiError, AzureNotFoundError
from meho_backplane.connectors.azure._http import NoRedirectClient, send
from meho_backplane.connectors.azure._paths import SUBSCRIPTIONS_API_VERSION, subscription_path
from meho_backplane.connectors.azure.session import (
    AzureCredentials,
    AzureCredentialsLoader,
    AzureTargetConfig,
    AzureTargetConfigError,
    AzureTargetLike,
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

# The family mixin classes. Each family imports its mixin on the line under its
# own comment: from meho_backplane.connectors.azure.<family> import <Family>Ops
# Ruff does not sort this part, so two families never add a line at the same place.
# Keep the empty line under each family: Ruff wants one between two imports.
# isort: off
# aks (Task #5087)
from meho_backplane.connectors.azure.aks import AksOps

# arc (Task #5084)
from meho_backplane.connectors.azure.arc import ArcOps

# automation (Task #5092)
from meho_backplane.connectors.azure.automation import AutomationOps

# avs (Task #5091)
from meho_backplane.connectors.azure.avs import AvsOps

# compute (Task #5083)
from meho_backplane.connectors.azure.compute import ComputeOps

# cost (Task #5088)
from meho_backplane.connectors.azure.cost import CostOps

# inventory (Task #5082)
from meho_backplane.connectors.azure.inventory import InventoryOps

# migrate (Task #5093)
from meho_backplane.connectors.azure.migrate import MigrateOps

# monitor (Task #5090)

# network (Task #5085)

# security (Task #5089)
from meho_backplane.connectors.azure.security import SecurityOps

# storage (Task #5086)
from meho_backplane.connectors.azure.storage import StorageOps
# isort: on

__all__ = ["AzureConnector"]

_log = structlog.get_logger(__name__)

#: Ask for a new token this many seconds before the old one runs out. Azure
#: tokens live 60 to 90 minutes; Microsoft's own SDK renews five minutes early.
_RENEW_BEFORE_EXPIRY_SECONDS: Final[float] = 300.0

#: The probe's description, stored with the fingerprint.
_PROBE_METHOD: Final[str] = "GET /subscriptions/{subscriptionId} (Azure Resource Manager)"

#: Errors that mean "the target cannot be read now". The fingerprint turns them
#: into ``reachable=False`` with the reason, instead of failing the probe.
_UNREACHABLE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    httpx.HTTPError,
    OSError,
    VaultClientError,
    CredentialsReadError,
    AzureTargetConfigError,
    MicrosoftLoginError,
    ConnectorAuthError,
    AzureApiError,
    AzureNotFoundError,
)

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


class AzureConnector(
    # Family mixin classes first, each on the line under its own comment below;
    # the HTTP base last. The mixins must stay before HttpConnector, so that a
    # mixin can replace a method of the base (the inventory family replaces
    # list_candidates). Their order among themselves does not matter today:
    # no two families define a method with the same name.
    # aks (Task #5087)
    AksOps,
    # arc (Task #5084)
    ArcOps,
    # automation (Task #5092)
    AutomationOps,
    # avs (Task #5091)
    AvsOps,
    # compute (Task #5083)
    ComputeOps,
    # cost (Task #5088)
    CostOps,
    # inventory (Task #5082)
    InventoryOps,
    # migrate (Task #5093)
    MigrateOps,
    # monitor (Task #5090)
    # network (Task #5085)
    # security (Task #5089)
    SecurityOps,
    # storage (Task #5086)
    StorageOps,
    HttpConnector,
):
    """Azure Resource Manager connector. One target is one subscription.

    Registered as ``("azure", "2026.10", "azure-arm")``, so the connector ID is
    ``azure-arm-2026.10``, plus the ``("azure", "", "")`` row that lets every
    ``azure`` target find it. Azure has no product version, so
    ``supported_version_range`` stays ``None``.
    """

    product = "azure"
    version = "2026.10"
    impl_id = "azure-arm"
    supported_version_range: str | None = None
    priority = 1
    #: No redirect is followed: every subscription shares one host, so even a
    #: redirect on the same host could carry the token elsewhere.
    _client_class = NoRedirectClient

    def __init__(self, *, credentials_loader: AzureCredentialsLoader | None = None) -> None:
        super().__init__()
        self._credentials_loader: AzureCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )
        self._credentials: dict[_CredentialKey, AzureCredentials] = {}
        self._tokens: dict[_TokenKey, _CachedToken] = {}
        self._credential_locks: dict[_CredentialKey, asyncio.Lock] = {}
        self._token_locks: dict[_TokenKey, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Login and cache
    # ------------------------------------------------------------------

    async def auth_headers(self, target: AzureTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}`` for a call to *target*."""
        headers, _login = await self.authorize(target, operator)
        return headers

    async def _access_token(
        self, target: AzureTargetLike, operator: Operator
    ) -> MicrosoftAccessToken:
        """Return a token for *target*, from the cache or from a new login."""
        token, _login = await self._token_and_login(target, operator)
        return token

    async def authorize(
        self, target: AzureTargetLike, operator: Operator, *, scope: str | None = None
    ) -> tuple[dict[str, str], tuple[str, str]]:
        """Return the ``Authorization`` header and the login (Azure tenant ID, client ID).

        The token is for *scope*, the API it is sent to; without it, for
        Resource Manager. The transport uses the login to apply the limit per
        Azure login.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"AzureConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target {target.name!r} "
                f"asked for auth_model={auth_model!r}"
            )
        token, login = await self._token_and_login(target, operator, scope=scope)
        return {"Authorization": f"Bearer {token.value}"}, login

    async def _token_and_login(
        self, target: AzureTargetLike, operator: Operator, *, scope: str | None = None
    ) -> tuple[MicrosoftAccessToken, tuple[str, str]]:
        """Return a token for *target* and the login it belongs to.

        The token is for *scope*; without it, for Resource Manager. The cache
        keeps one token per scope, so a token for one API never goes to
        another (the storage reads ask for Azure Storage's scope).
        """
        config = resolve_target_config(target)
        credentials = await self._credentials_for(target, operator)
        check_tenant_match(target, config, credentials)
        login = (credentials.tenant_id, credentials.client_id)
        scope = config.cloud.arm_scope if scope is None else scope
        key: _TokenKey = (*target_cache_key(target), *login, scope)
        use_cache = _may_use_cache(operator)
        cached = self._tokens.get(key)
        if use_cache and cached is not None and _monotonic() < cached.renew_at:
            return cached.token, login
        async with self._token_locks.setdefault(key, asyncio.Lock()):
            cached = self._tokens.get(key)
            if use_cache and cached is not None and _monotonic() < cached.renew_at:
                return cached.token, login
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
                "azure_login_done",
                target=target.name,
                cloud=config.cloud.name,
                azure_tenant_id=credentials.tenant_id,
                client_id=credentials.client_id,
                expires_in=token.expires_in,
            )
            return token, login

    async def _credentials_for(
        self, target: AzureTargetLike, operator: Operator
    ) -> AzureCredentials:
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

    async def invalidate_session(
        self, target: AzureTargetLike, *, rejected: httpx.Request | None = None
    ) -> None:
        """Drop the target's cached tokens (the dispatcher's 401 hook).

        After Azure answers 401 the dispatcher calls this and tries the call
        once more. The retry then logs in again. The cached secret stays: a
        401 means the token is no longer accepted, not that the secret is
        wrong.

        The token is per target, so *rejected* (the request that got the
        ``401``) is not needed and not read.
        """
        self._drop_tokens(target)

    async def invalidate_credentials(self, target: AzureTargetLike) -> None:
        """Drop the target's cached secret and tokens (the failed-login hook).

        The dispatcher calls this when Microsoft refuses the login. The next
        call reads the secret again, so a corrected secret works without a
        restart.
        """
        prefix = target_cache_key(target)
        for key in [key for key in self._credentials if key[:2] == prefix]:
            del self._credentials[key]
        self._drop_tokens(target)

    def _drop_tokens(self, target: AzureTargetLike) -> None:
        prefix = target_cache_key(target)
        for key in [key for key in self._tokens if key[:2] == prefix]:
            del self._tokens[key]

    async def aclose(self) -> None:
        """Forget every cached secret and token, then close the HTTP clients."""
        self._credentials.clear()
        self._tokens.clear()
        await super().aclose()

    # ------------------------------------------------------------------
    # Azure Resource Manager reads
    # ------------------------------------------------------------------

    async def arm_get(
        self,
        target: AzureTargetLike,
        path: str,
        *,
        operator: Operator,
        api_version: str,
    ) -> dict[str, Any]:
        """GET *path* on the target's Resource Manager host and return the JSON body.

        Goes through :func:`~meho_backplane.connectors.azure._http.send`: one
        retry budget, a wait when Azure throttles, one new login after a 401.
        """
        response = await send(
            self, target, "GET", path, operator=operator, params={"api-version": api_version}
        )
        return json_payload_or_empty(response)

    async def read_subscription(
        self, target: AzureTargetLike, operator: Operator
    ) -> tuple[AzureTargetConfig, dict[str, Any]]:
        """Read the target's subscription. Returns the checked settings and the answer."""
        config = resolve_target_config(target)
        payload = await self.arm_get(
            target,
            subscription_path(config.subscription_id),
            operator=operator,
            api_version=SUBSCRIPTIONS_API_VERSION,
        )
        return config, payload

    async def login_identity(
        self, target: AzureTargetLike, operator: Operator
    ) -> MicrosoftAccessToken:
        """Return the token the next call to *target* uses, to show its ids."""
        return await self._access_token(target, operator)

    # ------------------------------------------------------------------
    # Fingerprint and probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: AzureTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read the subscription and describe the target.

        ``vendor="microsoft"``, ``product="azure"``. Azure has no product
        version, so ``version`` stays ``None``. ``extras`` holds the
        subscription ID, the tenant ID, the cloud and the subscription state.
        A call without an operator runs as MEHO's system identity, which cannot
        read the secret, so it reports ``reachable=False`` with the reason.
        """
        probed_at = datetime.now(UTC)
        effective_operator = operator if operator is not None else synthesise_system_operator()
        try:
            config, payload = await self.read_subscription(target, effective_operator)
        except _UNREACHABLE_ERRORS as exc:
            return FingerprintResult(
                vendor="microsoft",
                product="azure",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="microsoft",
            product="azure",
            version=None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_PROBE_METHOD,
            extras={
                "subscription_id": payload.get("subscriptionId"),
                "tenant_id": payload.get("tenantId"),
                "cloud": config.cloud.name,
                "state": payload.get("state"),
            },
        )

    async def probe(self, target: AzureTargetLike) -> ProbeResult:
        """Check the target with the same subscription read as :meth:`fingerprint`."""
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
        target: AzureTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Old entry point. Sends the call through the dispatcher.

        Callers today use :func:`meho_backplane.operations.dispatch` directly.
        This path runs as MEHO's system identity, which cannot read the
        target's secret, so an Azure call through it fails on purpose.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:azure-arm-connector-shim",
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
    # Typed operations
    # ------------------------------------------------------------------

    async def about(
        self, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``azure.about``: the subscription and the login of this target."""
        from meho_backplane.connectors.azure.typed_reads import azure_about_impl

        return await azure_about_impl(self, operator, target, params)

    async def location_list(
        self, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``azure.location.list``: the Azure regions of this target's subscription."""
        from meho_backplane.connectors.azure.typed_reads import azure_location_list_impl

        return await azure_location_list_impl(self, operator, target, params)

    async def operation_status(
        self, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``azure.operation.status``: read the state of a long-running operation once."""
        from meho_backplane.connectors.azure.typed_reads import azure_operation_status_impl

        return await azure_operation_status_impl(self, operator, target, params)
