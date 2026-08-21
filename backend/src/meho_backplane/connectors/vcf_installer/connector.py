# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""InstallerConnector — hand-rolled HttpConnector subclass for VCF Installer 9.1.

Auth + fingerprint + probe + the G0.6 dispatch shim + the bound-method shims
for the four governed submit/poll bring-up primitives (#3078 — bodies in
:mod:`.typed_reads` / :mod:`.typed_writes`). The one-shot governed bring-up
(validate → deploy as a single approved unit) is the separate ``dangerous`` +
``requires_approval`` composite in :mod:`.bringup`; this connector supplies the
session-auth POST twin (:meth:`_post_json_with_session_retry`) it dispatches
through.

Registered against the v2 registry at import time in
:mod:`meho_backplane.connectors.vcf_installer.__init__` under
``(product="installer", version="9.1", impl_id="installer-rest")``.

Auth — token session derived from the shipped ExecutionProfile
--------------------------------------------------------------

The Installer flow is the ``session_login_token`` named scheme (#2287), the same
as SDDC Manager: ``POST /v1/tokens`` with a ``{username, password}`` body
(``TokenCreationSpec``) returns ``accessToken`` (``TokenPair``), sent as
``Authorization: Bearer <accessToken>`` on every subsequent request. The
connector derives its login path, request headers, and session-expiry status set
from :data:`~meho_backplane.connectors.vcf_installer.profile.INSTALLER_EXECUTION_PROFILE`
via that scheme's spec, so the mechanics cannot drift.

The per-target session token is cached (single-flight under a lock). On a
downstream ``401`` the cached token is evicted and the login re-run once: the
:meth:`_get_json_with_session_retry` helper covers the fingerprint/probe
round-trip, and the public :meth:`invalidate_session` hook is the seam the
generic dispatch path calls (#2067) before re-dispatching once. Credentials are
loaded once per target from Vault (a ``401`` means the *token* expired, not the
credential).

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or ``None``
for pre-G0.3 targets); any other ``target.auth_model`` raises a clear
:exc:`NotImplementedError`.

Fingerprint
-----------

``GET /v1/system/appliance-info`` returns a flat ``ApplianceInfo`` object with a
top-level ``version`` string and ``role`` — the Installer appliance's own version
(it has no deployed inventory to fingerprint from). The GET authenticates via the
token session, so it goes through :meth:`_get_json_with_session_retry`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.profile_auth import SESSION_SCHEME_SPECS
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vcf_auth import SessionLoginError, vcf_session_login
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.vcf_installer.profile import INSTALLER_EXECUTION_PROFILE
from meho_backplane.connectors.vcf_installer.session import (
    InstallerCredentialsLoader,
    InstallerTargetLike,
    load_credentials_from_vault,
)

__all__ = ["InstallerConnector", "SessionLoginError"]

_log = structlog.get_logger(__name__)

# The ``session_login_token`` scheme spec (#2287), selected by the shipped
# profile — supplies the login path, request headers, credential-body builder,
# and token extractor from one declaration.
_SESSION_SPEC = SESSION_SCHEME_SPECS[INSTALLER_EXECUTION_PROFILE.auth.scheme]

# ``POST /v1/tokens`` — sourced from the scheme spec's login-path builder.
_SESSION_CREATE_PATH = _SESSION_SPEC.login_path(INSTALLER_EXECUTION_PROFILE.auth)

# Static headers the login POST carries (JSON Content-Type / Accept).
_SESSION_REQUEST_HEADERS: dict[str, str] = dict(_SESSION_SPEC.request_headers)

# Downstream statuses meaning "cached token no longer accepted; re-login once".
_SESSION_EXPIRED_STATUSES: frozenset[int] = INSTALLER_EXECUTION_PROFILE.expiry_statuses


def _is_acceptable_auth_model(value: Any) -> bool:
    """True iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset (``None``)."""
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


def _installer_login_body(username: str, password: str) -> dict[str, Any]:
    """Build the Installer ``{username, password}`` login body via the scheme spec."""
    return dict(
        _SESSION_SPEC.build_body(
            INSTALLER_EXECUTION_PROFILE.auth,
            {"username": username, "password": password},
        )
    )


def _extract_access_token(resp: httpx.Response) -> str | None:
    """Pull ``accessToken`` out of the Installer ``TokenPair`` response via the scheme spec."""
    try:
        payload = resp.json()
    except ValueError:
        return None
    token = _SESSION_SPEC.extract_token(payload)
    return token.token if token is not None else None


class InstallerConnector(HttpConnector):
    """VCF Installer 9.1 REST connector with profile-derived token-session auth."""

    # G0.6 v2 registry metadata. ``"installer-rest-9.1"`` ->
    # (product="installer", version="9.1", impl_id="installer-rest").
    product = "installer"
    version = "9.1"
    impl_id = "installer-rest"
    supported_version_range = ">=9.1,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: InstallerCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._creds_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._session_tokens: dict[tuple[str, str], str] = {}
        self._session_lock = asyncio.Lock()
        self._credentials_loader: InstallerCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def auth_headers(self, target: InstallerTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <accessToken>"}`` for the request.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is anything
        other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"InstallerConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {"Authorization": f"Bearer {token}"}

    async def _session_token(self, target: InstallerTargetLike, operator: Operator) -> str:
        """Return the cached session token for *target*, establishing on first use.

        The lock serialises concurrent first-use callers so two don't both pay
        the ``POST /v1/tokens`` round-trip. The cache fast-path is closed to the
        synthesised system operator (``is_system_operator``): a system caller
        always re-establishes and its token is never written to the shared cache
        (#1008).
        """
        cache_key = target_cache_key(target)
        async with self._session_lock:
            cached = self._session_tokens.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            creds = await self._load_credentials(target, operator)
            client = await self._http_client(target)
            token = await vcf_session_login(
                client,
                _SESSION_CREATE_PATH,
                username=creds["username"],
                password=creds["password"],
                target_name=target.name,
                payload_builder=_installer_login_body,
                token_extractor=_extract_access_token,
                request_headers=dict(_SESSION_REQUEST_HEADERS),
                request_extensions=self._request_extensions(target),
            )
            if not is_system_operator(operator):
                self._session_tokens[cache_key] = token
            _log.info("installer_session_established", target=target.name, host=target.host)
            return token

    async def invalidate_session(self, target: InstallerTargetLike) -> None:
        """Public duck-typed session-eviction hook for the dispatch path (#2067)."""
        await self._invalidate_session(target)

    async def _invalidate_session(self, target: InstallerTargetLike) -> None:
        """Drop the cached session token for *target* (credential cache left intact)."""
        async with self._session_lock:
            self._session_tokens.pop(target_cache_key(target), None)

    async def invalidate_credentials(self, target: InstallerTargetLike) -> None:
        """Public duck-typed credential-eviction hook for the dispatch path (#2396)."""
        async with self._creds_lock:
            self._creds_cache.pop(target_cache_key(target), None)

    async def _get_json_with_session_retry(
        self,
        target: InstallerTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET *path* with single session-expiry → re-login → retry-once recovery."""
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _SESSION_EXPIRED_STATUSES:
                raise
            await self._invalidate_session(target)
        try:
            return await self._get_json(target, path, operator=operator, params=params)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in _SESSION_EXPIRED_STATUSES:
                raise RuntimeError(
                    f"vcf-installer session re-login failed for target {target.name!r}: "
                    f"GET {path} returned HTTP {status_code} after refresh"
                ) from exc
            raise

    async def _post_json_with_session_retry(
        self,
        target: InstallerTargetLike,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
        verb: str = "POST",
    ) -> dict[str, Any]:
        """POST *path* with single session-expiry → re-login → retry-once recovery.

        The write-path twin of :meth:`_get_json_with_session_retry`, used by the
        governed bring-up composite. Retrying a non-idempotent verb is safe *only*
        for a session-expiry status: a ``401`` means the request was rejected at
        auth **before** the server processed it, so the first attempt had no
        effect and re-issuing once after a re-login cannot double-apply the write.
        Any other status propagates unretried.
        """
        try:
            return await self._post_json(target, path, operator=operator, verb=verb, json=json)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _SESSION_EXPIRED_STATUSES:
                raise
            await self._invalidate_session(target)
        try:
            return await self._post_json(target, path, operator=operator, verb=verb, json=json)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in _SESSION_EXPIRED_STATUSES:
                raise RuntimeError(
                    f"vcf-installer session re-login failed for target {target.name!r}: "
                    f"{verb} {path} returned HTTP {status_code} after refresh"
                ) from exc
            raise

    async def _load_credentials(
        self, target: InstallerTargetLike, operator: Operator
    ) -> dict[str, str]:
        """Return the cached credentials for *target*, loading from Vault on first use.

        The loaded dict must contain ``"username"`` and ``"password"``; a missing
        key raises :exc:`RuntimeError` naming the target and key. The cache
        fast-path is closed to the synthesised system operator (#1008).
        """
        cache_key = target_cache_key(target)
        async with self._creds_lock:
            cached = self._creds_cache.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            raw = await self._credentials_loader(target, operator)
            try:
                _ = raw["username"]
                _ = raw["password"]
            except KeyError as exc:
                raise RuntimeError(
                    f"vcf-installer credentials loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            self._creds_cache[cache_key] = raw
            _log.info("installer_credentials_loaded", target=target.name, host=target.host)
            return raw

    async def fingerprint(
        self,
        target: InstallerTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /v1/system/appliance-info``.

        Reads the flat ``ApplianceInfo`` object's top-level ``version`` and
        ``role``. The GET authenticates via the token session through
        :meth:`_get_json_with_session_retry`, so an idle-expired token is
        recovered with one re-login. On transport/status failure returns a
        non-reachable result whose ``extras["error"]`` carries the exception.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            payload = await self._get_json_with_session_retry(
                target, "/v1/system/appliance-info", operator=eff_operator
            )
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="installer",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /v1/system/appliance-info",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="vmware",
            product="installer",
            version=payload.get("version"),
            build=None,
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /v1/system/appliance-info",
            extras={
                "role": payload.get("role"),
                "dns_domain": payload.get("dnsDomain"),
            },
        )

    async def probe(self, target: InstallerTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check — delegates to :meth:`fingerprint`."""
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
        target: InstallerTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher. Post-G0.6 callers
        construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly."""
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:installer-rest-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        connector_id = f"{self.impl_id}-{self.version}"
        return await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )

    async def sddc_spec_validate(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.sddc.spec.validate`` shim — SddcSpec validation dry-run submit."""
        from meho_backplane.connectors.vcf_installer.typed_writes import (
            installer_sddc_spec_validate_impl,
        )

        return await installer_sddc_spec_validate_impl(self, operator, target, params)

    async def sddc_validation_status(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.sddc.validation.status`` shim — spec validation poll."""
        from meho_backplane.connectors.vcf_installer.typed_reads import (
            installer_sddc_validation_status_impl,
        )

        return await installer_sddc_validation_status_impl(self, operator, target, params)

    async def sddc_bringup_start(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.sddc.bringup.start`` shim — governed bring-up submit."""
        from meho_backplane.connectors.vcf_installer.typed_writes import (
            installer_sddc_bringup_start_impl,
        )

        return await installer_sddc_bringup_start_impl(self, operator, target, params)

    async def sddc_bringup_status(
        self, operator: Operator, target: InstallerTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``installer.sddc.bringup.status`` shim — bring-up task poll."""
        from meho_backplane.connectors.vcf_installer.typed_reads import (
            installer_sddc_bringup_status_impl,
        )

        return await installer_sddc_bringup_status_impl(self, operator, target, params)

    async def aclose(self) -> None:
        """Clear cached session tokens + credentials, then tear down the httpx pool."""
        async with self._session_lock:
            self._session_tokens.clear()
        async with self._creds_lock:
            self._creds_cache.clear()
        await super().aclose()
