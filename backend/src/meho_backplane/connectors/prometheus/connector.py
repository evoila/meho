# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PrometheusConnector -- read-only HttpConnector over the Prometheus HTTP API.

Initiative #2228 / Task #2234. One connector for the three
PromQL-HTTP-compatible metrics backends an estate runs -- **Prometheus**,
**Thanos Query**, and **Grafana Mimir/Cortex** -- all of which expose the
same ``/api/v1`` HTTP API. The connector is read-only *by construction*:

* **GET-only + path-allowlist.** Every dispatched request goes through
  :meth:`PrometheusConnector._api_get`, which rejects any method other
  than GET and any path that does not start with ``/api/v1/`` *before*
  touching the wire. Admin, TSDB-delete (``POST /api/v1/admin/tsdb/*``),
  and lifecycle (``/-/reload``, ``/-/quit``) endpoints are therefore
  unreachable through dispatch -- there is no code path that issues a
  mutating verb.

* **Optional auth.** In-cluster Prometheus is typically reached via
  port-forward and is unauthenticated, so ``target.secret_ref = None``
  is a first-class state: :meth:`auth_headers` returns ``{}`` (no
  credential load attempted) in that case. When ``secret_ref`` *is* set,
  the connector reads the KV-v2 secret and sends ``Authorization:
  Bearer`` (secret field ``token``) or ``Authorization: Basic`` (fields
  ``username`` + ``password``), chosen by which fields the operator
  stored -- the same field-shape discriminator the gh-rest connector
  uses. This "auth optional when ``secret_ref is None``" branch is
  net-new: every other connector's execute path fails closed on an
  unset ``secret_ref``.

* **Scheme + path prefix are per-target.** The base
  :meth:`HttpConnector._base_url` hardcodes ``https``; a port-forwarded
  in-cluster Prometheus is plain ``http`` on ``:9090``, and Mimir mounts
  the Prometheus API under a ``/prometheus`` path prefix. Both are read
  from ``target.extras`` (``extras["scheme"]`` in ``{http, https}``,
  default ``https``; ``extras["path_prefix"]``, default none) so no new
  Target column is needed. The prefix is applied to the wire path
  (:meth:`_wire_path`) rather than folded into ``base_url``, because an
  absolute request path (``/api/v1/...``) would otherwise replace a
  ``base_url`` that carried the prefix.

Fingerprint
-----------

:meth:`fingerprint` reads ``GET /api/v1/status/buildinfo`` (the canonical
reachability + version signal) and best-effort augments it with ``/-/ready``,
scrape-target count, firing-alert count, and rule-group count. It also
surfaces a ``flavour`` hint (``prometheus`` | ``thanos`` | ``mimir``).

The buildinfo payload is **byte-identical** across the three backends --
Thanos and Mimir vendor Prometheus's ``PrometheusVersion`` struct
(``version``/``revision``/``branch``/``buildUser``/``buildDate``/``goVersion``),
so a Thanos or Mimir target is *not* self-identifying from its API
response. The ``flavour`` is therefore operator-asserted via
``target.extras["flavour"]`` (defaulting to ``prometheus``); the operator
knows which of the three they deployed. This is deliberate substrate
minimalism -- no unreliable heuristic sniffing.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.prometheus.ops import PROMETHEUS_OPS
from meho_backplane.connectors.prometheus.ops_read import PrometheusReadOps
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = [
    "PrometheusConnector",
    "PrometheusReadOnlyError",
    "PrometheusSecretLoader",
]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import
# Target` once G0.3's Target model rollout reaches the connectors.
type Target = Any

#: The read-only allowlist. Every dispatched path must start here; the GET
#: verb is the only method the connector ever issues. Lifecycle endpoints
#: (``/-/reload``, ``/-/quit``) live *outside* ``/api/v1/`` and are
#: rejected by the allowlist.
_ALLOWED_PATH_PREFIX = "/api/v1/"

#: Explicit blocklist *inside* the allowlist. Prometheus's admin HTTP API
#: (TSDB-delete / clean-tombstones / snapshot) is mounted under
#: ``/api/v1/admin/`` and is POST-only -- the GET-only gate already
#: neutralises it, but the path is blocklisted too so "admin is
#: unreachable" holds by construction even against a backend that might
#: answer a GET there.
_BLOCKED_PATH_PREFIXES = ("/api/v1/admin/",)

#: Valid ``flavour`` hints. One connector serves all three; the operator
#: asserts which backend a target is via ``target.extras["flavour"]``.
_VALID_FLAVOURS = frozenset({"prometheus", "thanos", "mimir"})

#: Buildinfo path (identical on Prometheus, Thanos Query, Mimir).
_BUILDINFO_PATH = "/api/v1/status/buildinfo"

#: Readiness path -- *not* under ``/api/v1/``; probed directly, best-effort.
_READY_PATH = "/-/ready"


@runtime_checkable
class PrometheusTargetLike(Protocol):
    """Structural type for the target fields the connector reads."""

    name: str
    host: str
    port: int | None
    secret_ref: str | None
    extras: Any


#: Injectable secret loader. Reads *target*'s KV-v2 secret under the
#: operator's identity and returns the raw field dict. Defaults to
#: :func:`load_vault_secret_data`; tests inject a fake to exercise the
#: Bearer/Basic selection without a live Vault.
PrometheusSecretLoader = Callable[[Any, Operator], Awaitable[dict[str, object]]]


async def _default_secret_loader(target: Any, operator: Operator) -> dict[str, object]:
    """Default loader -- operator-context KV-v2 read via the shared helper."""
    return await load_vault_secret_data(target, operator)


class PrometheusReadOnlyError(ValueError):
    """Raised when a dispatched request would leave the read-only surface.

    Either the method is not GET or the path does not start with
    ``/api/v1/``. Raised *before* any upstream call so a misuse of the
    ``prometheus.get`` passthrough (or a future coding error) can never
    reach a mutating endpoint.
    """


def _enforce_read_only(method: str, logical_path: str) -> None:
    """Assert *method*/*logical_path* stay on the read-only surface.

    The gate is deliberately structural: GET-only **and** a
    ``/api/v1/`` path-allowlist. ``logical_path`` is the API-relative
    path (before any per-target prefix is applied), compared against the
    allowlist minus its query string. A leading traversal segment
    (``..``) is rejected too, so a crafted ``/api/v1/../-/reload`` cannot
    escape the prefix check.
    """
    if method.upper() != "GET":
        raise PrometheusReadOnlyError(
            f"prometheus connector is read-only: method {method!r} is not permitted (GET only)"
        )
    path_only = logical_path.split("?", 1)[0]
    if not path_only.startswith(_ALLOWED_PATH_PREFIX):
        raise PrometheusReadOnlyError(
            f"prometheus connector is read-only: path {logical_path!r} is outside the "
            f"{_ALLOWED_PATH_PREFIX!r} allowlist"
        )
    if any(path_only.startswith(blocked) for blocked in _BLOCKED_PATH_PREFIXES):
        raise PrometheusReadOnlyError(
            f"prometheus connector is read-only: path {logical_path!r} is on the admin "
            f"blocklist (admin / TSDB-delete endpoints are unreachable by construction)"
        )
    if ".." in path_only.split("/"):
        raise PrometheusReadOnlyError(
            f"prometheus connector rejects path traversal in {logical_path!r}"
        )


class PrometheusConnector(PrometheusReadOps, HttpConnector):
    """Read-only connector over the Prometheus ``/api/v1`` HTTP API.

    Registry v2 triple: ``("prometheus", "2.x", "prometheus-api")``. The
    same class serves Thanos Query and Mimir/Cortex (PromQL-HTTP-
    compatible); the operator asserts which via ``target.extras["flavour"]``.

    :attr:`priority` is ``1`` so a future ``GenericRestConnector``
    auto-shim (priority ``0``) loses the registry tie-break if both
    register for the same triple.
    """

    product = "prometheus"
    version = "2.x"
    impl_id = "prometheus-api"
    supported_version_range = None
    priority = 1

    def __init__(self, *, secret_loader: PrometheusSecretLoader | None = None) -> None:
        super().__init__()
        self._secret_loader: PrometheusSecretLoader = (
            secret_loader if secret_loader is not None else _default_secret_loader
        )

    # ------------------------------------------------------------------
    # Transport shaping: scheme + per-target path prefix
    # ------------------------------------------------------------------

    def _scheme(self, target: Target) -> str:
        """Return ``http`` or ``https`` for *target* (default ``https``).

        Read from ``target.extras["scheme"]``. A port-forwarded in-cluster
        Prometheus is plain ``http`` on ``:9090``; targets behind a TLS
        ingress (the common Thanos/Mimir shape) keep the secure default.
        An unrecognised value falls back to the secure default.
        """
        extras = getattr(target, "extras", None) or {}
        scheme = str(extras.get("scheme", "https")).lower()
        return scheme if scheme in ("http", "https") else "https"

    def _path_prefix(self, target: Target) -> str:
        """Return the API mount prefix for *target* (default empty).

        Read from ``target.extras["path_prefix"]``. Mimir mounts the
        Prometheus API under ``/prometheus`` by default; a reverse proxy
        may mount vanilla Prometheus under an arbitrary prefix. Normalised
        to a leading slash with no trailing slash (``""`` when unset).
        """
        extras = getattr(target, "extras", None) or {}
        prefix = str(extras.get("path_prefix", "") or "").strip()
        if not prefix:
            return ""
        prefix = "/" + prefix.strip("/")
        return prefix

    def _base_url(self, target: Target) -> str:
        """Return ``<scheme>://<host>[:<port>]`` -- overrides the https-only base.

        The per-target path prefix is *not* folded in here: httpx resolves
        an absolute request path (``/api/v1/...``) against ``base_url`` by
        replacing the base path entirely, which would drop the prefix. The
        prefix is applied to the wire path in :meth:`_wire_path` instead.
        """
        scheme = self._scheme(target)
        default_port = 443 if scheme == "https" else 80
        port = f":{target.port}" if target.port and target.port != default_port else ""
        return f"{scheme}://{target.host}{port}"

    def _wire_path(self, target: Target, logical_path: str) -> str:
        """Prepend the per-target API mount prefix to *logical_path*."""
        return f"{self._path_prefix(target)}{logical_path}"

    # ------------------------------------------------------------------
    # Auth (optional): {} when secret_ref is None, else Bearer / Basic
    # ------------------------------------------------------------------

    async def auth_headers(self, target: Target, operator: Operator) -> dict[str, str]:
        """Return the request auth headers -- empty when no credential is set.

        The net-new "auth optional when ``secret_ref is None``" branch:
        an unauthenticated port-forward target carries no credential, so
        no Vault read is attempted and no ``Authorization`` header is
        sent. When ``target.secret_ref`` *is* set, the KV-v2 secret is
        read under the operator's identity and the scheme is chosen by the
        stored fields: a ``token`` field -> ``Authorization: Bearer``; a
        ``username`` + ``password`` pair -> ``Authorization: Basic``.

        Raises :class:`VaultCredentialsReadError` when a secret is set but
        carries neither shape.
        """
        if getattr(target, "secret_ref", None) is None:
            return {}
        secret_data = await self._secret_loader(target, operator)
        return {"Authorization": self._authorization_value(target, secret_data)}

    def _authorization_value(self, target: Target, secret_data: dict[str, object]) -> str:
        """Build the ``Authorization`` value from *secret_data*'s field shape."""
        if "token" in secret_data:
            token = strip_credential_value(secret_data["token"])
            return f"Bearer {token}"
        if "username" in secret_data and "password" in secret_data:
            username = strip_credential_value(secret_data["username"])
            password = strip_credential_value(secret_data["password"])
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
            return f"Basic {encoded}"
        raise VaultCredentialsReadError(
            f"prometheus secret for target {getattr(target, 'name', target)!r} carries "
            f"neither a 'token' field (Bearer) nor a 'username'+'password' pair (Basic); "
            f"cannot build an Authorization header"
        )

    # ------------------------------------------------------------------
    # Gated request helpers
    # ------------------------------------------------------------------

    async def _api_get(
        self,
        target: Target,
        logical_path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET *logical_path* after the read-only gate, applying the prefix.

        The single seam every dispatched op flows through. The gate
        (:func:`_enforce_read_only`) runs on the API-relative
        ``logical_path`` -- so the ``/api/v1/`` allowlist is checked
        against the caller's declared path, not the prefixed wire path --
        and the per-target prefix is applied only afterwards. Delegates to
        the base :meth:`HttpConnector._request_json` (idempotent-GET retry
        + ``auth_headers``).
        """
        _enforce_read_only("GET", logical_path)
        wire_path = self._wire_path(target, logical_path)
        return await self._get_json(target, wire_path, operator=operator, params=params)

    async def _ready(self, target: Target, operator: Operator) -> bool | None:
        """Best-effort ``GET /-/ready`` -- ``True``/``False`` or ``None`` on error.

        ``/-/ready`` is outside the ``/api/v1/`` allowlist (it is the
        server's own readiness probe, not a data endpoint) and returns
        plain text, so it bypasses :meth:`_api_get` and hits the pooled
        client directly. Any transport error maps to ``None`` (unknown) so
        a missing endpoint -- Mimir exposes ``/ready``, not ``/-/ready`` --
        never fails the fingerprint.
        """
        try:
            client = await self._http_client(target)
            headers = await self.auth_headers(target, operator)
            resp = await client.request(
                "GET",
                self._wire_path(target, _READY_PATH),
                headers=headers,
                extensions=self._request_extensions(target),
            )
        except (httpx.HTTPError, OSError, VaultClientError, VaultCredentialsReadError):
            return None
        return resp.status_code == 200

    # ------------------------------------------------------------------
    # Flavour hint (operator-asserted)
    # ------------------------------------------------------------------

    def _flavour(self, target: Target) -> str:
        """Return the operator-asserted backend flavour (default ``prometheus``).

        Read from ``target.extras["flavour"]``. Buildinfo cannot
        distinguish the three backends (Thanos/Mimir vendor Prometheus's
        version struct verbatim), so the operator asserts it. An
        unrecognised value falls back to ``prometheus``.
        """
        extras = getattr(target, "extras", None) or {}
        flavour = str(extras.get("flavour", "prometheus")).lower()
        return flavour if flavour in _VALID_FLAVOURS else "prometheus"

    # ------------------------------------------------------------------
    # Fingerprint / probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint from ``/api/v1/status/buildinfo`` (+ extras).

        Buildinfo is the reachability + version signal (required for
        ``reachable=True``). The ``flavour`` hint, ``/-/ready`` state, and
        the scrape-target / firing-alert / rule-group counts are
        best-effort augmentations -- each is populated when reachable and
        left ``None`` otherwise (Thanos Query has no scrape targets;
        counts may 404 depending on the backend).

        ``operator`` is optional for ABC parity; a background caller
        passes ``None`` and the read runs under the synthesised system
        operator. For the common unauthenticated port-forward target
        (``secret_ref=None``) no credential is needed either way.
        """
        probed_at = datetime.now(UTC)
        eff_operator = operator or synthesise_system_operator()
        flavour = self._flavour(target)

        try:
            buildinfo = await self._api_get(target, _BUILDINFO_PATH, operator=eff_operator)
        except (
            httpx.HTTPError,
            OSError,
            VaultClientError,
            VaultCredentialsReadError,
            PrometheusReadOnlyError,
        ) as exc:
            _log.warning(
                "prometheus_fingerprint_unreachable",
                target=getattr(target, "name", None),
                flavour=flavour,
                error=f"{type(exc).__name__}: {exc}",
            )
            return FingerprintResult(
                vendor="prometheus",
                product="prometheus",
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {_BUILDINFO_PATH}",
                extras={"error": f"{type(exc).__name__}: {exc}", "flavour": flavour},
            )

        return await self._reachable_fingerprint(
            target, eff_operator, buildinfo, flavour, probed_at
        )

    async def _reachable_fingerprint(
        self,
        target: Target,
        operator: Operator,
        buildinfo: dict[str, Any],
        flavour: str,
        probed_at: datetime,
    ) -> FingerprintResult:
        """Assemble the reachable :class:`FingerprintResult` from a buildinfo body.

        Extracts ``version`` / ``revision`` / ``branch`` / ``goVersion``
        from the buildinfo ``data`` block and augments them with the
        best-effort ``/-/ready`` state and the scrape-target /
        firing-alert / rule-group counts (each ``None`` when unavailable).
        """
        data = buildinfo.get("data", {}) if isinstance(buildinfo, dict) else {}
        if not isinstance(data, dict):
            data = {}
        version = data.get("version")
        return FingerprintResult(
            vendor="prometheus",
            product="prometheus",
            version=str(version) if version is not None else None,
            edition=flavour,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {_BUILDINFO_PATH}",
            extras={
                "flavour": flavour,
                "revision": data.get("revision"),
                "branch": data.get("branch"),
                "go_version": data.get("goVersion"),
                "ready": await self._ready(target, operator),
                "active_targets": await self._best_effort_active_targets(target, operator),
                "firing_alerts": await self._best_effort_firing_alerts(target, operator),
                "rule_groups": await self._best_effort_rule_groups(target, operator),
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability check via ``GET /api/v1/status/buildinfo``.

        Returns ``ok=True`` when buildinfo is fetched. Any transport,
        status, auth, or gate failure maps to ``ok=False`` with the
        exception class + message as ``reason`` -- the probe never raises
        (#986). Runs under the synthesised system operator; an
        unauthenticated target needs no credential.
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)
        operator = synthesise_system_operator()
        try:
            await self._api_get(target, _BUILDINFO_PATH, operator=operator)
        except (
            httpx.HTTPError,
            OSError,
            VaultClientError,
            VaultCredentialsReadError,
            PrometheusReadOnlyError,
        ) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.monotonic() - start) * 1000.0,
                probed_at=probed_at,
            )
        return ProbeResult(
            ok=True,
            latency_ms=(time.monotonic() - start) * 1000.0,
            probed_at=probed_at,
        )

    # ------------------------------------------------------------------
    # Registration + dispatch shim
    # ------------------------------------------------------------------

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`PROMETHEUS_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan (via the registrar queued in
        :mod:`meho_backplane.connectors.prometheus.__init__`) after the
        registry has eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.prometheus.ops.PROMETHEUS_OPS`,
        resolves each op's ``handler_attr`` to the bound method, looks the
        group's curated ``when_to_use`` up, and routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across restarts -- mirrors the argocd / bind9 shape.
        """
        from meho_backplane.connectors.prometheus.ops import (
            PROMETHEUS_WHEN_TO_USE_BY_GROUP,
        )
        from meho_backplane.operations.typed_register import register_typed_operation

        for op in PROMETHEUS_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"PrometheusConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = PROMETHEUS_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"PrometheusConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use exists "
                        f"for that key. Add an entry to PROMETHEUS_WHEN_TO_USE_BY_GROUP "
                        f"in meho_backplane.connectors.prometheus.ops."
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
            "prometheus_operations_registered",
            count=len(PROMETHEUS_OPS),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(
        self,
        target: Target,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Mirrors :meth:`HetznerRobotConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``) construct a
        real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly -- they do not
        reach this method. The connector's natural key is encoded as the
        dispatcher's ``connector_id``: ``"prometheus-api-2.x"``.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator as _Operator
        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = _Operator(
            sub="system:prometheus-api-connector-shim",
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
