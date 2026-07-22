# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""ProxmoxConnector — hand-rolled HttpConnector subclass for Proxmox VE 8.x (#2238).

The one **read/write** connector of Initiative #2228 (the other five are
read-only). Over the Proxmox VE REST API (``/api2/json`` on :8006, self-signed
TLS by default), it ships a generic ``<METHOD> <path>`` passthrough split into
a read op (``proxmox.api.get``) and an approval-gated write op
(``proxmox.api.write``), a ``proxmox.about`` fingerprint wrapper, and a
``proxmox.task.status`` UPID poll. There is **no code-level GET gate**: write
authorisation leans entirely on MEHO's policy gate + approval queue — the
write op registers ``requires_approval=True`` and its handler runs only on the
``_approved=True`` resume path.

Auth (token preferred over ticket)
==================================

Credential resolution lives in
:mod:`meho_backplane.connectors.proxmox.session`; this connector turns the
resolved :class:`~meho_backplane.connectors.proxmox.session.ProxmoxCredentials`
into request auth:

* **API token** — ``Authorization: PVEAPIToken=<token_id>=<token_secret>`` on
  every request. CSRF-exempt, so writes need no extra header. Preferred.
* **Ticket** — one ``POST /api2/json/access/ticket`` mints a ticket + a
  ``CSRFPreventionToken``; the ticket rides as the ``PVEAuthCookie`` cookie
  and the CSRF token is attached on every write (ticket auth is *not*
  CSRF-exempt). Tickets last ~2h; the minted ticket is cached and is **not**
  auto-re-minted on expiry — a 401 after expiry surfaces as a
  ``connector_error`` (transparent re-mint is a documented future
  follow-up). Token auth (preferred) has no such expiry.

Self-signed TLS
===============

Inherited from :class:`HttpConnector`: the operator pins a CA
(``tls_ca_pin`` — verification stays on) or opts out per-target
(``verify_tls=false``). No connector-level TLS handling is needed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.proxmox.ops import (
    PROXMOX_READ_OPS,
    READ_METHODS,
    join_api_path,
    validate_api_path,
    validate_method,
)
from meho_backplane.connectors.proxmox.session import (
    ProxmoxCredentials,
    ProxmoxCredentialsLoader,
    ProxmoxTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["ProxmoxConnector"]

_log = structlog.get_logger(__name__)

#: Proxmox REST paths used by the fingerprint. Both require authentication —
#: unlike ArgoCD's /api/version, PVE has no unauthenticated identity endpoint.
_VERSION_PATH = f"{join_api_path('version')}"
_NODES_PATH = f"{join_api_path('nodes')}"
_TICKET_PATH = join_api_path("access/ticket")
_PROBE_METHOD = f"GET {_VERSION_PATH}"

#: Terminal Proxmox task status. A task's ``status`` is ``running`` until the
#: task finishes, then ``stopped`` (with ``exitstatus`` carrying the outcome).
_TERMINAL_TASK_STATUS = "stopped"
_DEFAULT_TASK_POLL_TIMEOUT = 300
_TASK_POLL_INTERVAL = 2.0


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Proxmox credentials (API token or username/password) are shared
    service-account material, so the connector locks to
    :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or ``None`` for pre-G0.3
    targets whose column is unpopulated) — the Harbor / argocd boundary.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


class ProxmoxConnector(HttpConnector):
    """Proxmox VE 8.x REST connector — token/ticket auth, approval-gated writes.

    Per-target credential material and (for ticket auth) the minted session
    are cached under the tenant-unique ``(tenant_id, target.id)`` key so two
    same-named targets in different tenants never share a token or ticket.
    """

    product = "proxmox"
    version = "8.x"
    impl_id = "proxmox-api"
    supported_version_range = ">=7.0,<9.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: ProxmoxCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._creds_cache: dict[tuple[str, str], ProxmoxCredentials] = {}
        #: Ticket-auth session cache: ``{"ticket": ..., "csrf": ...}`` per target.
        self._ticket_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._ticket_lock = asyncio.Lock()
        self._credentials_loader: ProxmoxCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def auth_headers(self, target: ProxmoxTargetLike, operator: Operator) -> dict[str, str]:
        """Return the request auth headers for *target* under *operator*.

        Token auth → ``Authorization: PVEAPIToken=<id>=<secret>``. Ticket auth
        → ``Cookie: PVEAuthCookie=<ticket>`` (minting the ticket on first
        use). Writes add the ``CSRFPreventionToken`` in :meth:`_write_json`;
        it is omitted here so GET/HEAD reads stay header-minimal and the
        CSRF-exempt token path never carries a spurious CSRF header.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is anything
        other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"ProxmoxConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._resolve_creds(target, operator)
        if creds.mode == "token":
            return {"Authorization": f"PVEAPIToken={creds.token_id}={creds.token_secret}"}
        session = await self._ticket_session(target, operator, creds)
        return {"Cookie": f"PVEAuthCookie={session['ticket']}"}

    async def _resolve_creds(
        self, target: ProxmoxTargetLike, operator: Operator
    ) -> ProxmoxCredentials:
        """Return the cached credential material, loading from Vault on first use.

        The cache fast path is closed to the synthesised system operator
        (``is_system_operator``) so a background/operator-less caller always
        re-runs the loader (and its fail-closed Vault guard) and can never be
        served credentials a real operator primed — the argocd/#1008
        discipline.
        """
        cache_key = target_cache_key(target)
        async with self._creds_lock:
            cached = self._creds_cache.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            creds = await self._credentials_loader(target, operator)
            self._creds_cache[cache_key] = creds
            _log.info(
                "proxmox_credentials_loaded",
                target=target.name,
                host=target.host,
                auth_mode=creds.mode,
            )
            return creds

    async def _ticket_session(
        self,
        target: ProxmoxTargetLike,
        operator: Operator,
        creds: ProxmoxCredentials,
    ) -> dict[str, str]:
        """Return a cached ``{ticket, csrf}``, minting one via the login POST.

        POSTs ``username`` + ``password`` (form-encoded) to
        ``/api2/json/access/ticket`` and stores the returned ``ticket`` +
        ``CSRFPreventionToken``. Uses the pooled client directly — the login
        itself sends no auth header. The system-operator fast-path carve-out
        mirrors :meth:`_resolve_creds`.
        """
        cache_key = target_cache_key(target)
        async with self._ticket_lock:
            cached = self._ticket_cache.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            client = await self._http_client(target)
            resp = await client.post(
                _TICKET_PATH,
                data={"username": creds.username, "password": creds.password},
                extensions=self._request_extensions(target),
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            session = {
                "ticket": str(data.get("ticket", "")),
                "csrf": str(data.get("CSRFPreventionToken", "")),
            }
            self._ticket_cache[cache_key] = session
            _log.info("proxmox_ticket_minted", target=target.name, host=target.host)
            return session

    async def _write_json(
        self,
        target: ProxmoxTargetLike,
        method: str,
        path: str,
        *,
        operator: Operator,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a mutating (POST/PUT/DELETE) request — never retried.

        Attaches the ``CSRFPreventionToken`` header when the target
        authenticates by ticket (token auth is CSRF-exempt). A non-2xx raises
        :exc:`httpx.HTTPStatusError` (the dispatcher's ``connector_error``
        branch records it). An empty / 204 body parses to ``{}``.
        """
        verb = method.upper()
        if verb not in {"POST", "PUT", "DELETE"}:
            raise ValueError(f"_write_json only accepts POST/PUT/DELETE; got {verb!r}")
        headers = await self.auth_headers(target, operator)
        creds = await self._resolve_creds(target, operator)
        if creds.mode == "ticket":
            session = await self._ticket_session(target, operator, creds)
            headers["CSRFPreventionToken"] = session["csrf"]
        client = await self._http_client(target)
        resp = await client.request(
            verb,
            path,
            data=data or None,
            params=params or None,
            headers=headers,
            extensions=self._request_extensions(target),
        )
        resp.raise_for_status()
        if not resp.content:
            return {}
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    # ------------------------------------------------------------------
    # Fingerprint / probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: ProxmoxTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint from ``GET /version`` + ``GET /nodes``.

        ``version`` becomes the canonical version; ``release`` and the
        ``repoid`` build hash plus a per-node ``[{node, status}]`` list land
        under ``extras``. Both endpoints require authentication (PVE has no
        unauthenticated identity endpoint), so a background call with
        ``operator=None`` runs under the synthesised system operator; on a
        Vault backend that fails closed at the live JWT round-trip →
        ``reachable=False``. Transport / auth / credential failures map to
        ``reachable=False`` with ``extras["error"]`` — the credential arm
        catches the backend-neutral ``CredentialsReadError``, so a ``gsm:``
        read failure degrades the same way a Vault one does.
        """
        effective_operator = operator or synthesise_system_operator()
        probed_at = datetime.now(UTC)
        try:
            version_payload = await self._get_json(
                target, _VERSION_PATH, operator=effective_operator
            )
            nodes_payload = await self._get_json(target, _NODES_PATH, operator=effective_operator)
        except (
            httpx.HTTPError,
            OSError,
            VaultClientError,
            CredentialsReadError,
        ) as exc:
            return FingerprintResult(
                vendor="proxmox",
                product="proxmox",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )

        version_data = version_payload.get("data") if isinstance(version_payload, dict) else None
        version_data = version_data if isinstance(version_data, dict) else {}
        return FingerprintResult(
            vendor="proxmox",
            product="proxmox",
            version=version_data.get("version") or None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_PROBE_METHOD,
            extras={
                "release": version_data.get("release"),
                "repoid": version_data.get("repoid"),
                "nodes": _node_status_list(nodes_payload),
            },
        )

    async def probe(self, target: ProxmoxTargetLike) -> ProbeResult:
        """Reachability check delegating to :meth:`fingerprint`.

        The probe route carries no operator, so the fingerprint's Vault read
        runs under the synthesised system operator and fails closed on a
        credential-guarded endpoint — the pfSense/argocd precedent. A
        reachable fingerprint maps to ``ok=True``; an unreachable one carries
        the structured error as ``reason``.
        """
        probed_at = datetime.now(UTC)
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=probed_at)
        return ProbeResult(
            ok=False,
            reason=str(fp.extras.get("error")) if fp.extras.get("error") else "unreachable",
            probed_at=probed_at,
        )

    # ------------------------------------------------------------------
    # Read op handlers
    # ------------------------------------------------------------------

    async def about(
        self,
        operator: Operator,
        target: ProxmoxTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``proxmox.about`` — version/repoid/per-node identity snapshot."""
        del params  # schema declares the param object empty
        fp = await self.fingerprint(target, operator)
        if not fp.reachable:
            raise httpx.HTTPError(str(fp.extras.get("error") or "proxmox target unreachable"))
        return {
            "vendor": fp.vendor,
            "product": fp.product,
            "version": fp.version,
            "release": fp.extras.get("release"),
            "repoid": fp.extras.get("repoid"),
            "reachable": True,
            "nodes": fp.extras.get("nodes"),
        }

    async def api_get(
        self,
        operator: Operator,
        target: ProxmoxTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``proxmox.api.get`` — allowlisted GET/HEAD passthrough.

        Re-validates path + method (handler-layer allowlist), prepends the
        constant ``/api2/json`` base, and returns the endpoint's ``data``
        payload (GET) or a ``{reachable, status_code}`` marker (HEAD).
        """
        rel_path = validate_api_path(str(params["path"]))
        method = validate_method(str(params.get("method", "GET")), READ_METHODS)
        query = params.get("query")
        query = query if isinstance(query, dict) else None
        wire_path = join_api_path(rel_path)
        if method == "HEAD":
            client = await self._http_client(target)
            headers = await self.auth_headers(target, operator)
            resp = await client.head(
                wire_path,
                params=query,
                headers=headers,
                extensions=self._request_extensions(target),
            )
            resp.raise_for_status()
            return {"reachable": True, "status_code": resp.status_code}
        payload = await self._get_json(target, wire_path, operator=operator, params=query)
        data = payload.get("data") if isinstance(payload, dict) else payload
        return {"data": data}

    async def api_write(
        self,
        operator: Operator,
        target: ProxmoxTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Bound-method shim for ``proxmox.api.write`` (approval-gated)."""
        from meho_backplane.connectors.proxmox.ops_write import proxmox_api_write

        return await proxmox_api_write(self, operator, target, params)

    async def task_status(
        self,
        operator: Operator,
        target: ProxmoxTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``proxmox.task.status`` — read (or poll) a UPID task to completion.

        Reads ``GET /nodes/{node}/tasks/{upid}/status``. With ``wait=true``,
        polls until ``status == "stopped"`` (terminal) or the bounded timeout
        elapses, returning the last-observed status with ``timed_out=True`` on
        expiry.
        """
        node = validate_api_path(str(params["node"]))
        upid = validate_api_path(str(params["upid"]))
        wire_path = join_api_path(f"nodes/{node}/tasks/{upid}/status")
        wait = bool(params.get("wait", False))
        timeout = int(params.get("poll_timeout_seconds") or _DEFAULT_TASK_POLL_TIMEOUT)

        async def _read_once() -> dict[str, Any]:
            payload = await self._get_json(target, wire_path, operator=operator)
            data = payload.get("data") if isinstance(payload, dict) else None
            return data if isinstance(data, dict) else {}

        if not wait:
            data = await _read_once()
            return {**data, "timed_out": False}

        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = await _read_once()
            if last.get("status") == _TERMINAL_TASK_STATUS:
                return {**last, "timed_out": False}
            await asyncio.sleep(_TASK_POLL_INTERVAL)
        return {**last, "timed_out": True}

    # ------------------------------------------------------------------
    # Registration + dispatch shim
    # ------------------------------------------------------------------

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every read + write op into ``endpoint_descriptor``.

        Walks :data:`PROXMOX_READ_OPS` + ``PROXMOX_WRITE_OPS``, resolves each
        op's ``handler_attr`` to its bound handler, looks the group's curated
        ``when_to_use`` blurb up in the merged read/write map, and routes each
        row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across pod restarts.
        """
        from meho_backplane.connectors.proxmox.ops import PROXMOX_WHEN_TO_USE_BY_GROUP
        from meho_backplane.connectors.proxmox.ops_write import (
            PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP,
            PROXMOX_WRITE_OPS,
        )
        from meho_backplane.operations.typed_register import register_typed_operation

        read_count = len(PROXMOX_READ_OPS)
        write_count = len(PROXMOX_WRITE_OPS)

        when_to_use_by_group = {
            **PROXMOX_WHEN_TO_USE_BY_GROUP,
            **PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP,
        }

        for op in (*PROXMOX_READ_OPS, *PROXMOX_WRITE_OPS):
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"ProxmoxConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = when_to_use_by_group.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"ProxmoxConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use "
                        f"exists for that key. Add an entry to "
                        f"PROXMOX_WHEN_TO_USE_BY_GROUP (ops.py) or "
                        f"PROXMOX_WHEN_TO_USE_WRITE_BY_GROUP (ops_write.py)."
                    )
            await register_typed_operation(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=op.op_id,
                handler=handler,
                summary=op.summary,
                description=op.description,
                parameter_schema=op.parameter_schema,
                response_schema=op.response_schema,
                group_key=op.group_key,
                when_to_use=when_to_use,
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "proxmox_operations_registered",
            count=read_count + write_count,
            read_count=read_count,
            write_count=write_count,
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(
        self,
        target: ProxmoxTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`ArgoCdConnector.execute`. Post-G0.6 callers construct a
        real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly. The connector's
        natural key encodes as ``"proxmox-api-8.x"`` per ``parse_connector_id``.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:proxmox-api-connector-shim",
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

    async def invalidate_credentials(self, target: ProxmoxTargetLike) -> None:
        """Duck-typed credential-eviction hook for the dispatch path (#2396).

        Drops the cached credentials for *target* so the next credential read
        re-reads Vault. Called by the dispatcher on an establish-auth failure
        so an operator's out-of-band restage converges on the next dispatch
        without a backplane restart. Holds :attr:`_creds_lock` so the pop is
        serialised against an in-flight load.
        """
        async with self._creds_lock:
            self._creds_cache.pop(target_cache_key(target), None)

    async def aclose(self) -> None:
        """Clear cached credentials + tickets, then tear down the httpx pool."""
        async with self._creds_lock:
            self._creds_cache.clear()
        async with self._ticket_lock:
            self._ticket_cache.clear()
        await super().aclose()


def _node_status_list(nodes_payload: Any) -> list[dict[str, Any]]:
    """Return ``[{node, status}]`` from a ``GET /nodes`` payload.

    ``GET /api2/json/nodes`` returns ``{"data": [{"node": ..., "status":
    "online"|"offline", ...}, ...]}``. Non-list / malformed payloads yield an
    empty list rather than raising — the fingerprint stays best-effort on the
    per-node detail while the version block is authoritative.
    """
    data = nodes_payload.get("data") if isinstance(nodes_payload, dict) else None
    if not isinstance(data, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict):
            result.append({"node": entry.get("node"), "status": entry.get("status")})
    return result
