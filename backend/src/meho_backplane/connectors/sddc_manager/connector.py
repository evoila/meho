# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SddcManagerConnector — hand-rolled HttpConnector subclass for SDDC Manager 9.0.

Skeleton-only — auth + fingerprint + probe + the G0.6 dispatch shim.
Operations arrive in #617 via G0.7 spec ingestion against the VCF API
spec for SDDC Manager.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.sddc_manager.__init__`. The G0.7 auto-shim's
idempotency check (in
:func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`
once #408's pipeline lands in main) no-ops on subsequent ingests against the
same ``(product="sddc", version="9.0", impl_id="sddc-rest")`` triple.

Auth divergence from the NSX/vSphere precedents
------------------------------------------------

SDDC Manager uses HTTP Basic auth sent on every request — no session cookie
or XSRF token is established. The connector caches the raw service-account
credentials (loaded once from Vault via an injectable loader) and computes
the ``Authorization: Basic`` header on each :meth:`auth_headers` call from
the cached values. The username is formatted as ``username@sso_realm`` where
``sso_realm`` defaults to ``"vsphere.local"`` per the consumer wrapper
contract; operators managing a custom SSO domain override this via
``target.sso_realm``.

Because HTTP Basic credentials are stateless server-side (no session to
revoke or expire), no 401-driven re-login loop is needed. A 401 from a
downstream call propagates directly to the caller — it means wrong
credentials, not an expired session.

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or
``None`` for pre-G0.3 targets where the column hasn't been populated yet).
:meth:`auth_headers` rejects any other ``target.auth_model`` value with a
clear :exc:`NotImplementedError` naming both the target and the requested
mode.

Fingerprint
-----------

``GET /v1/sddc-managers`` returns a pagination envelope
``{"elements": [{id, fqdn, version, domain: {id, name}, ...}], ...}``.
:meth:`fingerprint` reads ``elements[0]`` (SDDC Manager is typically a
singleton appliance). ``version`` carries the full version string (e.g.
``"5.2.0.0-24276214"``); ``build`` is extracted from a separate ``build``
field when present (VCF 9.x may surface it explicitly), otherwise ``None``.
``extras["management_domain"]`` carries the management domain name.

Operations
----------

This module ships zero operations — the G0.6 dispatch shim :meth:`execute`
exists for ABC compatibility but operations land in the
``endpoint_descriptor`` table via #617's spec ingestion. Until then, the
connector is registered and discoverable but ``execute(target, op_id, ...)``
against any ``op_id`` resolves to "unknown operation" at the dispatcher
layer — which is the correct behaviour for a registered-but-empty connector
at this Task's stage.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.sddc_manager.session import (
    SddcCredentialsLoader,
    SddcTargetLike,
    load_credentials_from_vault,
)

__all__ = ["SddcManagerConnector"]

_log = structlog.get_logger(__name__)

_DEFAULT_SSO_REALM = "vsphere.local"


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    "auth_model column not yet populated" sentinel for pre-G0.3 targets).
    Any other value is rejected by the caller. Same predicate the NSX and
    vSphere precedents use; lifted into this module to keep connectors
    decoupled.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


def _basic_auth_header(username: str, password: str) -> str:
    """Compute the ``Authorization: Basic`` header value for *username*:*password*."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


class SddcManagerConnector(HttpConnector):
    """SDDC Manager 9.0 REST connector with HTTP Basic auth.

    Per-target credentials cached in :attr:`_creds_cache` (loaded once via
    the injectable :class:`SddcCredentialsLoader`). HTTP Basic auth is sent
    on every request via ``Authorization: Basic <base64>`` — no session
    token is established and no 401-driven re-login is needed.

    The :attr:`priority` is set to ``1`` so a future
    ``GenericRestConnector`` auto-shim that somehow registers for the same
    triple loses the registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"sddc-rest-9.0"`` -> (``"sddc"``, ``"9.0"``, ``"sddc-rest"``).
    product = "sddc"
    version = "9.0"
    impl_id = "sddc-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: SddcCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._creds_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._credentials_loader: SddcCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def auth_headers(self, target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Basic ..."}`` for the request.

        Loads credentials from Vault on first call against *target*, caches
        them, and reuses the cached values on subsequent calls. The full
        ``operator`` is forwarded to :meth:`_load_credentials` so the live
        default loader (G3.10-T1 #945) reads the per-target Vault secret
        under the operator's identity (``vault_client_for_operator(operator)``).
        :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` selects the Vault-sourced
        service account once the loader has resolved it; the operator's
        JWT only authenticates the read, not the SDDC Manager request
        itself.

        The Basic auth username is ``{creds['username']}@{target.sso_realm}``
        where ``sso_realm`` defaults to ``"vsphere.local"`` when unset or
        empty.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"SddcManagerConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._load_credentials(target, operator)
        sso_realm = getattr(target, "sso_realm", None) or _DEFAULT_SSO_REALM
        auth_username = f"{creds['username']}@{sso_realm}"
        return {"Authorization": _basic_auth_header(auth_username, creds["password"])}

    async def _load_credentials(self, target: SddcTargetLike, operator: Operator) -> dict[str, str]:
        """Return the cached credentials for *target*, loading from Vault on first use.

        The lock serialises concurrent first-use callers for the same target;
        subsequent calls take the fast path under the same lock. The loaded
        dict must contain ``"username"`` and ``"password"`` keys; missing
        keys raise a :exc:`RuntimeError` naming the target and the missing
        key so operators can identify a misconfigured Vault path.

        ``operator`` is forwarded to the
        :class:`SddcCredentialsLoader` so the default loader can read
        the per-target Vault secret under the operator's identity
        (G3.10-T1's live read). The default loader is the thin
        sddc-manager-specific entry point to the shared
        operator-context Vault read; injected test loaders accept the
        same ``(target, operator)`` pair.

        The cache fast-path is closed to the synthesised system operator
        (``is_system_operator``): a system/operator-less caller always
        runs the loader so its fail-closed guard applies, and can never be
        served warm credentials a real operator primed but it could not
        resolve itself (#1008). Real-operator behaviour is unchanged —
        cold load → cache → reuse.
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
                    f"sddc-manager credentials loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            self._creds_cache[cache_key] = raw
            _log.info(
                "sddc_manager_credentials_loaded",
                target=target.name,
                host=target.host,
            )
            return raw

    async def fingerprint(
        self,
        target: SddcTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /v1/sddc-managers``.

        Reads ``elements[0]`` from the pagination envelope. On transport or
        status failure, returns a non-reachable :class:`FingerprintResult`
        whose ``extras["error"]`` carries the exception class + message —
        same pattern the NSX and vSphere connectors established.

        ``operator`` (optional) is the request-scoped operator forwarded
        from the probe routes. When provided, the credentials loader
        reads the per-target Vault secret under that identity — the
        same code path the dispatch surface uses. ``None`` falls back
        to a system operator whose placeholder JWT fails closed at the
        live Vault round-trip. G0.16-T4 (#1306) converged probe +
        dispatch on this signature; pre-fix the probe path hard-coded
        the placeholder JWT and surfaced as the v0.8.0 dogfood's
        ``malformed jwt: must have three parts`` finding on
        ``vcf9-sddc``.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        try:
            payload = await self._get_json(target, "/v1/sddc-managers", operator=eff_operator)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="sddc",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /v1/sddc-managers",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        elements = payload.get("elements") or []
        sddc = elements[0] if elements else {}
        domain = sddc.get("domain") or sddc.get("managementDomain") or {}
        return FingerprintResult(
            vendor="vmware",
            product="sddc",
            version=sddc.get("version"),
            build=sddc.get("build"),
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /v1/sddc-managers",
            extras={
                "id": sddc.get("id"),
                "fqdn": sddc.get("fqdn"),
                "management_domain": domain.get("name"),
                "management_domain_id": domain.get("id"),
            },
        )

    async def probe(self, target: SddcTargetLike) -> ProbeResult:
        """Lightweight reachability + auth-challenge check.

        Delegates to :meth:`fingerprint` — one authenticated request covers
        both reachability and auth-challenge, same posture the vSphere and
        NSX precedents use.
        """
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
        target: SddcTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`NsxConnector.execute`'s shape. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs
        once #618 lands) construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't
        reach this method.

        The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"sddc-rest-9.0"`` → (product=``"sddc"``,
        version=``"9.0"``, impl_id=``"sddc-rest"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:sddc-rest-connector-shim",
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

    async def aclose(self) -> None:
        """Clear cached credentials, then tear down the httpx pool.

        No server-side session to revoke — HTTP Basic is stateless.
        The credential cache is cleared so a post-aclose reuse of the same
        connector instance (e.g. a test that builds one connector across two
        contexts) starts clean.
        """
        async with self._creds_lock:
            self._creds_cache.clear()
        await super().aclose()
