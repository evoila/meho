# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""HcxConnector -- hand-rolled HttpConnector subclass for VMware HCX.

Login
-----

HCX Manager authenticates with a session token, not with cookies and not
with HTTP Basic (HCX 4.5 removed cookie authentication):

1. ``POST /hybridity/api/sessions`` with a JSON body
   ``{"username": ..., "password": ...}`` -- the vCenter single sign-on
   account registered with the HCX Manager, read from Vault through the
   :class:`~meho_backplane.connectors.hcx.session.HcxSessionLoader`.
2. The response header ``x-hm-authorization`` carries the token. It is
   cached per credential (``credential_cache_key``: tenant, secret and
   login host), so two targets that share one credential share one token.
3. Every later call sends the token in the ``x-hm-authorization`` header.
4. On ``401`` from a later call the dispatcher calls
   :meth:`invalidate_session` with the rejected request. The token is
   dropped only when that request sent it, so a late ``401`` never drops
   a newer token. The dispatcher then runs the call once more.

Version
-------

:meth:`manager_version` reads the HCX Manager version with
``POST /hybridity/api/hcspAppliances`` (body
``{"action": "query", "filter": {"local": "true"}}``), the call the vendor
upgrade guide names for "which version runs now". The first local
appliance row carries ``version``. The same helper backs
:meth:`fingerprint` and the ``hcx.api_file.get`` answer; the operation
``hcx.about`` reads the same call through :func:`parse_manager_version`.

Operations
----------

``hcx.api_file.get`` (the body lives in
:mod:`meho_backplane.connectors.hcx.api_file`) reads the API
documentation that a running HCX Manager serves, so the vendor API files
can be pinned. The other reads (``hcx.about``, site pairings, service
meshes, appliances, the service mesh diagnostics, jobs, alerts and the
inventory) live in :mod:`meho_backplane.connectors.hcx.typed_reads`.
The service mesh changes and the task read ``hcx.task.get`` live in
:mod:`meho_backplane.connectors.hcx.service_mesh_writes`. The network
extension operations (list, extend, unextend, Mobility Optimized
Networking) live in :mod:`meho_backplane.connectors.hcx.network_extension`.
The migration operations (validate, start, list, cancel, mobility groups
and the switchover window) live in
:mod:`meho_backplane.connectors.hcx.migrations`.

Traces
------

:meth:`trace_response_body` masks every secret-named field of a vendor
answer (the same rule the reads use) before the flight recorder records
it, so a stored trace never holds a pairing password or a token.
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
from meho_backplane.connectors._shared.rejected_token import drop_rejected_token
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vcf_auth import session_establish_auth_error
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.hcx.session import (
    HcxSessionLoader,
    HcxTargetLike,
    is_acceptable_auth_model,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["HcxConnector", "HcxManagerVersion"]

_log = structlog.get_logger(__name__)

#: Login endpoint. ``POST`` with a JSON ``{"username", "password"}`` body;
#: success is ``200`` with the token in the ``x-hm-authorization`` header.
_SESSION_PATH = "/hybridity/api/sessions"

#: Header that carries the session token on every authenticated call.
_AUTH_HEADER = "x-hm-authorization"

#: Version read. ``POST`` with :data:`_APPLIANCES_QUERY_BODY`; the local
#: appliance row carries ``version`` (and ``systemName`` / ``systemType``).
_APPLIANCES_PATH = "/hybridity/api/hcspAppliances"
_APPLIANCES_QUERY_BODY: dict[str, Any] = {"action": "query", "filter": {"local": "true"}}

#: How many parts the vendor version string has when it ends in the build
#: number (``4.3.0.0.19064502``: product version, then the build).
_VERSION_PARTS_WITH_BUILD = 5


@dataclass(frozen=True)
class HcxManagerVersion:
    """The HCX Manager version as :meth:`HcxConnector.manager_version` reads it.

    ``version`` is the vendor string as served. ``build`` is the vendor
    ``buildNumber`` field when the row has one, else the last part of a
    five-part version string. ``error`` names why the read failed; then
    ``version`` and ``build`` are ``None``.
    """

    version: str | None
    build: str | None
    error: str | None


def _appliance_rows(payload: Any) -> list[dict[str, Any]]:
    """Return the appliance rows of a ``hcspAppliances`` answer, in any known envelope."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return [row for row in value["items"] if isinstance(row, dict)]
    return [payload] if "version" in payload else []


def parse_manager_version(payload: Any) -> HcxManagerVersion:
    """Pick the version and build out of a ``hcspAppliances`` answer."""
    for row in _appliance_rows(payload):
        version = row.get("version")
        if not isinstance(version, str) or not version:
            continue
        build_raw = row.get("buildNumber")
        build = str(build_raw) if build_raw not in (None, "") else None
        if build is None:
            parts = version.split(".")
            if len(parts) == _VERSION_PARTS_WITH_BUILD and parts[-1].isdigit():
                build = parts[-1]
        return HcxManagerVersion(version=version, build=build, error=None)
    return HcxManagerVersion(
        version=None,
        build=None,
        error=f"POST {_APPLIANCES_PATH} returned no appliance row with a version",
    )


class HcxConnector(HttpConnector):
    """HCX REST connector with session-token auth (``x-hm-authorization``)."""

    product = "hcx"
    version = "9.0"
    impl_id = "hcx-rest"
    supported_version_range = ">=4.7,<10.0"
    priority = 1

    def __init__(self, *, session_loader: HcxSessionLoader | None = None) -> None:
        super().__init__()
        self._session_tokens: dict[tuple[str, ...], str] = {}
        self._session_lock = asyncio.Lock()
        self._session_loader: HcxSessionLoader = (
            session_loader if session_loader is not None else load_session_credentials_from_vault
        )

    async def auth_headers(self, target: HcxTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"x-hm-authorization": <token>}``, logging in on first use.

        Raises :exc:`NotImplementedError` when ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"HcxConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        token = await self._session_token(target, operator)
        return {_AUTH_HEADER: token}

    async def _session_token(self, target: HcxTargetLike, operator: Operator) -> str:
        """Return the cached session token for *target*, logging in on first use."""
        cache_key = credential_cache_key(target)
        async with self._session_lock:
            cached = self._session_tokens.get(cache_key)
            if cached is not None:
                return cached
            creds = await self._session_loader(target, operator)
            try:
                body = {"username": creds["username"], "password": creds["password"]}
            except KeyError as exc:
                raise RuntimeError(
                    f"hcx session loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            client = await self._http_client(target)
            try:
                resp = await client.post(
                    _SESSION_PATH,
                    json=body,
                    extensions=self._request_extensions(target),
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                message = (
                    f"hcx session establish failed for target {target.name!r}: "
                    f"POST {_SESSION_PATH} returned HTTP {exc.response.status_code}"
                )
                raise (
                    session_establish_auth_error(exc, message=message, target=target)
                    or RuntimeError(message)
                ) from exc
            token: str | None = resp.headers.get(_AUTH_HEADER)
            if not token:
                raise RuntimeError(
                    f"hcx session establish for target {target.name!r}: "
                    f"POST {_SESSION_PATH} returned 2xx with no {_AUTH_HEADER} response header"
                )
            self._session_tokens[cache_key] = token
            _log.info("hcx_session_established", target=target.name, host=target.host)
            return token

    async def invalidate_session(
        self, target: HcxTargetLike, *, rejected: httpx.Request | None = None
    ) -> None:
        """Drop the cached session token for *target* (the dispatcher's 401 hook).

        With *rejected*, the token is dropped only when that request sent
        it; a newer token stays. The next :meth:`auth_headers` call logs in
        again when the token was dropped. Credentials are not cached: every
        login reads them through the loader.
        """
        cache_key = credential_cache_key(target)
        async with self._session_lock:
            drop_rejected_token(
                self._session_tokens, cache_key, rejected, lambda token: {_AUTH_HEADER: token}
            )

    async def manager_version(self, target: HcxTargetLike, operator: Operator) -> HcxManagerVersion:
        """Read the HCX Manager version. Never raises: a failure is named in ``error``."""
        try:
            payload = await self._post_json(
                target, _APPLIANCES_PATH, operator=operator, json=_APPLIANCES_QUERY_BODY
            )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            return HcxManagerVersion(version=None, build=None, error=f"{type(exc).__name__}: {exc}")
        return parse_manager_version(payload)

    async def fingerprint(
        self,
        target: HcxTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from :meth:`manager_version`."""
        probed_at = datetime.now(UTC)
        eff_operator = operator if operator is not None else synthesise_system_operator()
        read = await self.manager_version(target, eff_operator)
        if read.error is not None:
            return FingerprintResult(
                vendor="vmware",
                product="hcx",
                reachable=False,
                probed_at=probed_at,
                probe_method=f"POST {_APPLIANCES_PATH}",
                extras={"error": read.error},
            )
        return FingerprintResult(
            vendor="vmware",
            product="hcx",
            version=read.version,
            build=read.build,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"POST {_APPLIANCES_PATH}",
        )

    async def probe(self, target: HcxTargetLike) -> ProbeResult:
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
        target: HcxTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the dispatcher (same shape as the nsx connector)."""
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:hcx-rest-connector-shim",
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

    async def api_file_get(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.api_file.get`` shim."""
        from meho_backplane.connectors.hcx.api_file import hcx_api_file_get_impl

        return await hcx_api_file_get_impl(self, operator, target, params)

    async def about(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.about`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_about_impl

        return await hcx_about_impl(self, operator, target, params)

    async def site_pairing_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.site_pairing.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_site_pairing_list_impl

        return await hcx_site_pairing_list_impl(self, operator, target, params)

    async def service_mesh_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_service_mesh_list_impl

        return await hcx_service_mesh_list_impl(self, operator, target, params)

    async def appliance_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.appliance.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_appliance_list_impl

        return await hcx_appliance_list_impl(self, operator, target, params)

    async def service_mesh_diagnose(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.diagnose`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_service_mesh_diagnose_impl

        return await hcx_service_mesh_diagnose_impl(self, operator, target, params)

    async def job_get(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.job.get`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_job_get_impl

        return await hcx_job_get_impl(self, operator, target, params)

    async def alert_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.alert.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_alert_list_impl

        return await hcx_alert_list_impl(self, operator, target, params)

    async def inventory_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.inventory.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_inventory_list_impl

        return await hcx_inventory_list_impl(self, operator, target, params)

    async def service_mesh_diagnostics_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.diagnostics.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import (
            hcx_service_mesh_diagnostics_list_impl,
        )

        return await hcx_service_mesh_diagnostics_list_impl(self, operator, target, params)

    async def service_mesh_diagnostics_get(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.diagnostics.get`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import (
            hcx_service_mesh_diagnostics_get_impl,
        )

        return await hcx_service_mesh_diagnostics_get_impl(self, operator, target, params)

    async def inventory_compute_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.inventory.compute.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_inventory_compute_list_impl

        return await hcx_inventory_compute_list_impl(self, operator, target, params)

    async def inventory_datastore_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.inventory.datastore.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_inventory_datastore_list_impl

        return await hcx_inventory_datastore_list_impl(self, operator, target, params)

    async def inventory_network_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.inventory.network.list`` shim."""
        from meho_backplane.connectors.hcx.typed_reads import hcx_inventory_network_list_impl

        return await hcx_inventory_network_list_impl(self, operator, target, params)

    async def service_mesh_create(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.create`` shim."""
        from meho_backplane.connectors.hcx.service_mesh_writes import hcx_service_mesh_create_impl

        return await hcx_service_mesh_create_impl(self, operator, target, params)

    async def service_mesh_update(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.update`` shim."""
        from meho_backplane.connectors.hcx.service_mesh_writes import hcx_service_mesh_update_impl

        return await hcx_service_mesh_update_impl(self, operator, target, params)

    async def service_mesh_resync(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.resync`` shim."""
        from meho_backplane.connectors.hcx.service_mesh_writes import hcx_service_mesh_resync_impl

        return await hcx_service_mesh_resync_impl(self, operator, target, params)

    async def service_mesh_delete(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.service_mesh.delete`` shim."""
        from meho_backplane.connectors.hcx.service_mesh_writes import hcx_service_mesh_delete_impl

        return await hcx_service_mesh_delete_impl(self, operator, target, params)

    async def task_get(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.task.get`` shim."""
        from meho_backplane.connectors.hcx.service_mesh_writes import hcx_task_get_impl

        return await hcx_task_get_impl(self, operator, target, params)

    async def network_extension_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.network_extension.list`` shim."""
        from meho_backplane.connectors.hcx.network_extension import (
            hcx_network_extension_list_impl,
        )

        return await hcx_network_extension_list_impl(self, operator, target, params)

    async def network_extension_extend(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.network_extension.extend`` shim."""
        from meho_backplane.connectors.hcx.network_extension import (
            hcx_network_extension_extend_impl,
        )

        return await hcx_network_extension_extend_impl(self, operator, target, params)

    async def network_extension_unextend(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.network_extension.unextend`` shim."""
        from meho_backplane.connectors.hcx.network_extension import (
            hcx_network_extension_unextend_impl,
        )

        return await hcx_network_extension_unextend_impl(self, operator, target, params)

    async def network_extension_mon_set(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.network_extension.mon.set`` shim."""
        from meho_backplane.connectors.hcx.network_extension import (
            hcx_network_extension_mon_set_impl,
        )

        return await hcx_network_extension_mon_set_impl(self, operator, target, params)

    async def migration_validate(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.migration.validate`` shim."""
        from meho_backplane.connectors.hcx.migrations import hcx_migration_validate_impl

        return await hcx_migration_validate_impl(self, operator, target, params)

    async def migration_start(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.migration.start`` shim."""
        from meho_backplane.connectors.hcx.migrations import hcx_migration_start_impl

        return await hcx_migration_start_impl(self, operator, target, params)

    async def migration_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.migration.list`` shim."""
        from meho_backplane.connectors.hcx.migrations import hcx_migration_list_impl

        return await hcx_migration_list_impl(self, operator, target, params)

    async def migration_cancel(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.migration.cancel`` shim."""
        from meho_backplane.connectors.hcx.migrations import hcx_migration_cancel_impl

        return await hcx_migration_cancel_impl(self, operator, target, params)

    async def mobility_group_list(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.mobility_group.list`` shim."""
        from meho_backplane.connectors.hcx.migrations import hcx_mobility_group_list_impl

        return await hcx_mobility_group_list_impl(self, operator, target, params)

    async def mobility_group_set(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.mobility_group.set`` shim."""
        from meho_backplane.connectors.hcx.migrations import hcx_mobility_group_set_impl

        return await hcx_mobility_group_set_impl(self, operator, target, params)

    async def migration_schedule(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.migration.schedule`` shim."""
        from meho_backplane.connectors.hcx.migrations import hcx_migration_schedule_impl

        return await hcx_migration_schedule_impl(self, operator, target, params)

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Mask the secret-named fields of a vendor answer before the trace records it.

        The flight recorder's own rules know secrets by shape (a bearer
        token, a key) and by op family (a login). A stored pairing password
        in ``GET /hybridity/api/cloudConfigs`` is neither, so the connector
        masks it here with the same rule the reads use. A body that is not
        JSON is recorded as the shared engine sees fit.
        """
        from meho_backplane.connectors.hcx.typed_reads import redact_secrets

        try:
            payload = response.json()
        except ValueError:
            return None
        return redact_secrets(payload)

    async def aclose(self) -> None:
        """Clear cached session tokens, then tear down the httpx pool."""
        async with self._session_lock:
            self._session_tokens.clear()
        await super().aclose()
