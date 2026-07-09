# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""LokiConnector -- read-only, multi-tenant HttpConnector for Grafana Loki (#2235).

The logs half of an LGTM stack, brought inside the MEHO dispatch ->
policy-gate -> audit seam. Registry v2 triple ``("loki", "3.x", "loki-api")``
over the Loki HTTP API (``/loki/api/v1``, default port 3100).

Design
------

* **Read-only by construction.** Every op issues a GET through
  :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._request_json`
  (which rejects non-idempotent verbs), and the generic ``loki.get``
  passthrough runs its path through
  :func:`~meho_backplane.connectors.loki.read_only.assert_loki_read_only`
  -- a GET-only, ``/loki/api/v1``-scoped gate with an explicit ``/push`` +
  ``/delete*`` blocklist. No write op is registered.

* **Multi-tenant via a per-call header.** Loki enforces tenancy with the
  ``X-Scope-OrgID`` header when ``auth_enabled``. No other MEHO connector
  threads a per-call tenant header, so it is modelled as an optional
  ``tenant`` op param rendered into ``extra_headers`` on each request -- not
  a target-level field and not part of :meth:`auth_headers` (so the
  readiness probe and fingerprint stay tenant-free). A query issued without
  a ``tenant`` against an ``auth_enabled`` Loki gets a ``401 "no org id"``;
  the connector translates that into a clear
  :class:`LokiTenantRequiredError` rather than passing the bare 401 through.

* **Optional auth.** Loki is commonly reached unauthenticated via a
  port-forward. When ``target.secret_ref`` is ``None`` the connector sends no
  ``Authorization`` header (:meth:`auth_headers` returns ``{}``); when it is
  set, the stored secret selects Bearer (a ``token`` field) or Basic
  (``username`` + ``password``). This is the explicit "auth optional when
  secret_ref is None" branch -- op execution otherwise fails closed on an
  unresolved credential.

* **Scheme.** Loki's native API is plain HTTP (the port-forward case), so the
  connector talks ``http`` by default. A TLS-fronted Loki is reached by
  setting ``extras={"scheme": "https"}`` on the target -- the one per-product
  field the base ``Target`` model does not model with a column, carried in the
  forward-compat ``extras`` bag.

* **Fingerprint.** ``GET /loki/api/v1/status/buildinfo`` (version + revision)
  plus ``GET /ready`` (readiness) plus a best-effort ``GET
  /loki/api/v1/labels`` (label_count). All three are issued tenant-free and
  unauthenticated, so the fingerprint works on a freshly registered target
  before any secret is configured -- the same precedent as the argocd
  ``/api/version`` probe.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.loki.ops import LOKI_OPS, LOKI_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.loki.read_only import assert_loki_read_only
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["LokiConnector", "LokiTenantRequiredError"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# once G0.3's Target model rollout lands, mirroring the sibling connectors.
type Target = Any

_BUILDINFO_PATH = "/loki/api/v1/status/buildinfo"
_READY_PATH = "/ready"
_LABELS_PATH = "/loki/api/v1/labels"
_TENANT_HEADER = "X-Scope-OrgID"


class LokiTenantRequiredError(RuntimeError):
    """A tenant-scoped Loki (``auth_enabled``) was queried without an org id.

    Raised when a query returns ``401`` and no ``tenant`` selector was
    supplied -- Loki answers ``"no org id"`` in that case. Surfacing an
    actionable error (rather than the bare 401) tells the operator to pass a
    ``tenant`` instead of chasing a credential problem. Subclasses
    :class:`RuntimeError` so the dispatcher's ``connector_error`` branch
    renders the message verbatim.
    """


def _version_retryable(exc: BaseException) -> bool:
    """Retry the unauthenticated probe GETs on connection errors and 5xx; never 4xx.

    Mirrors :func:`HttpConnector._retryable`; defined locally so the
    fingerprint/probe path (which bypasses the base ``_request_json`` retry
    wrapper because it sends no auth or tenant header) keeps the same
    idempotent-GET retry semantics without reaching into the adapter's private
    name.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


class LokiConnector(HttpConnector):
    """Grafana Loki read-only, multi-tenant connector over ``/loki/api/v1``.

    Registry v2 triple ``("loki", "3.x", "loki-api")``. ``priority`` is set to
    ``1`` so a future ``GenericRestConnector`` auto-shim registering the same
    triple loses the resolver tie-break ladder.
    """

    product = "loki"
    version = "3.x"
    impl_id = "loki-api"
    supported_version_range = ">=2.9,<4.0"
    priority = 1

    # ------------------------------------------------------------------
    # Transport plumbing
    # ------------------------------------------------------------------

    def _base_url(self, target: Target) -> str:
        """Return ``{scheme}://{host}[:{port}]`` -- HTTP by default.

        Loki's native API is plaintext HTTP (the port-forward case), so the
        default scheme is ``http``; a TLS-fronted Loki sets
        ``extras={"scheme": "https"}`` on the target. The port is appended
        unless it is the scheme's default (80 for http, 443 for https).
        """
        scheme = self._scheme(target)
        default_port = 443 if scheme == "https" else 80
        port = getattr(target, "port", None)
        port_suffix = f":{port}" if port and port != default_port else ""
        return f"{scheme}://{target.host}{port_suffix}"

    @staticmethod
    def _scheme(target: Target) -> str:
        """Return ``"https"`` when the target's ``extras`` opts in, else ``"http"``."""
        extras = getattr(target, "extras", None) or {}
        return "https" if str(extras.get("scheme", "")).lower() == "https" else "http"

    async def auth_headers(self, target: Target, operator: Operator) -> dict[str, str]:
        """Return the optional ``Authorization`` header -- ``{}`` when unauthenticated.

        Optional-auth branch: a target with ``secret_ref=None`` is
        unauthenticated (the port-forward case) and gets no header. When
        ``secret_ref`` is set, the stored KV-v2 secret selects the scheme -- a
        ``token`` field yields Bearer; ``username`` + ``password`` yields
        Basic. A configured-but-unusable secret (neither shape) raises
        :class:`VaultCredentialsReadError`.

        The tenant header is deliberately *not* set here -- it is per-call
        (:meth:`_loki_get`) so the probe and fingerprint stay tenant-free.
        """
        if not getattr(target, "secret_ref", None):
            return {}
        secret = await load_vault_secret_data(target, operator)
        token = secret.get("token")
        if token:
            return {"Authorization": f"Bearer {strip_credential_value(token)}"}
        username = secret.get("username")
        password = secret.get("password")
        if username is not None and password is not None:
            raw = f"{strip_credential_value(username)}:{strip_credential_value(password)}"
            encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        raise VaultCredentialsReadError(
            f"loki target {getattr(target, 'name', target)!r} has a secret_ref but its "
            "secret carries neither a 'token' (Bearer) nor 'username'+'password' (Basic) "
            "credential"
        )

    async def _loki_get(
        self,
        operator: Operator,
        target: Target,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        """Issue a gated, optionally tenant-scoped GET and return parsed JSON.

        Runs the read-only gate first (so a bad path never reaches the wire),
        renders ``tenant`` into ``X-Scope-OrgID`` when set, and translates a
        ``401`` on a tenant-less call into :class:`LokiTenantRequiredError`.
        """
        assert_loki_read_only("GET", path)
        extra_headers = {_TENANT_HEADER: tenant} if tenant else None
        try:
            return await self._request_json(
                target, "GET", path, operator=operator, params=params, extra_headers=extra_headers
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401 and tenant is None:
                raise LokiTenantRequiredError(
                    f"loki target {getattr(target, 'name', target)!r} returned 401 with no "
                    "tenant supplied; this Loki has auth_enabled (multi-tenant). Pass a "
                    "'tenant' selector so the request carries the X-Scope-OrgID header"
                ) from exc
            raise

    @staticmethod
    def _forward(params: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        """Return the subset of *params* whose *keys* are present and non-None."""
        return {k: params[k] for k in keys if params.get(k) is not None}

    # ------------------------------------------------------------------
    # Curated read ops -- each threads ``operator`` for the (optional) auth
    # read and an optional ``tenant`` for the X-Scope-OrgID header.
    # ------------------------------------------------------------------

    async def query(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``loki.query`` -- ``GET /loki/api/v1/query`` (LogQL instant query)."""
        query = {"query": params["query"], **self._forward(params, ("time", "limit", "direction"))}
        return await self._loki_get(
            operator, target, "/loki/api/v1/query", params=query, tenant=params.get("tenant")
        )

    async def query_range(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``loki.query_range`` -- ``GET /loki/api/v1/query_range`` (range query)."""
        query = {
            "query": params["query"],
            **self._forward(
                params, ("start", "end", "since", "step", "interval", "limit", "direction")
            ),
        }
        return await self._loki_get(
            operator, target, "/loki/api/v1/query_range", params=query, tenant=params.get("tenant")
        )

    async def labels(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``loki.labels`` -- ``GET /loki/api/v1/labels`` (known label names)."""
        query = self._forward(params, ("start", "end", "since", "query"))
        return await self._loki_get(
            operator, target, _LABELS_PATH, params=query or None, tenant=params.get("tenant")
        )

    async def label_values(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``loki.label_values`` -- ``GET /loki/api/v1/label/{name}/values``."""
        name = quote(str(params["name"]), safe="")
        query = self._forward(params, ("start", "end", "since", "query"))
        return await self._loki_get(
            operator,
            target,
            f"/loki/api/v1/label/{name}/values",
            params=query or None,
            tenant=params.get("tenant"),
        )

    async def series(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``loki.series`` -- ``GET /loki/api/v1/series`` (streams matching selectors)."""
        # httpx serialises the list value into repeated ``match[]=a&match[]=b``
        # pairs -- exactly Loki's expected wire shape for the selector param.
        query: dict[str, Any] = {"match[]": list(params["match"])}
        query.update(self._forward(params, ("start", "end", "since")))
        return await self._loki_get(
            operator, target, "/loki/api/v1/series", params=query, tenant=params.get("tenant")
        )

    async def get_passthrough(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``loki.get`` -- read-only GET passthrough to any ``/loki/api/v1`` path.

        The path is gated by :func:`assert_loki_read_only` inside
        :meth:`_loki_get` (GET-only, ``/loki/api/v1``-scoped, ``/push`` +
        ``/delete*`` blocked), so the passthrough can never mutate Loki.
        """
        path = str(params["path"])
        extra = params.get("params")
        return await self._loki_get(
            operator,
            target,
            path,
            params=extra if isinstance(extra, dict) and extra else None,
            tenant=params.get("tenant"),
        )

    # ------------------------------------------------------------------
    # Fingerprint / probe -- tenant-free and unauthenticated
    # ------------------------------------------------------------------

    async def _unauth_get(self, target: Target, path: str) -> httpx.Response:
        """Retried GET with **no** auth or tenant header, returning the response.

        The base :meth:`HttpConnector._get_json` always calls
        :meth:`auth_headers`; the probe/fingerprint path must not, so it hits
        the pooled client directly. Retry semantics match the base class:
        idempotent GET, 3 retries on connection errors / 5xx.
        """

        @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
            retry=retry_if_exception(_version_retryable),
            reraise=True,
        )
        async def _do_get() -> httpx.Response:
            client = await self._http_client(target)
            resp = await client.get(path)
            resp.raise_for_status()
            return resp

        return await _do_get()

    async def _best_effort_label_count(self, target: Target) -> int | None:
        """Return the tenant-free label count, or ``None`` when unavailable.

        ``GET /loki/api/v1/labels`` is tenant-scoped on an ``auth_enabled``
        Loki, so a tenant-free read there 401s -- treated as "unknown" (``None``)
        rather than a fingerprint failure.
        """
        try:
            resp = await self._unauth_get(target, _LABELS_PATH)
            payload = resp.json()
        except (httpx.HTTPError, OSError, ValueError):
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        return len(data) if isinstance(data, list) else None

    async def fingerprint(
        self, target: Target, operator: Operator | None = None
    ) -> FingerprintResult:
        """Canonical fingerprint from ``status/buildinfo`` + ``/ready`` + labels.

        ``buildinfo`` yields ``version`` and ``revision``; ``/ready`` yields
        the readiness flag; ``labels`` yields a best-effort ``label_count``. All
        three are tenant-free and unauthenticated, so the fingerprint works on
        a freshly registered target before any secret is configured. Transport
        or status failure on ``buildinfo`` maps to ``reachable=False`` with the
        error under ``extras``.
        """
        del operator  # buildinfo/ready are unauthenticated -- no per-operator read
        probed_at = datetime.now(UTC)
        try:
            build_resp = await self._unauth_get(target, _BUILDINFO_PATH)
            buildinfo = build_resp.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            return FingerprintResult(
                vendor="grafana",
                product="loki",
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {_BUILDINFO_PATH}",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        if not isinstance(buildinfo, dict):
            buildinfo = {}
        ready = await self._ready_flag(target)
        label_count = await self._best_effort_label_count(target)
        return FingerprintResult(
            vendor="grafana",
            product="loki",
            version=buildinfo.get("version") or None,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {_BUILDINFO_PATH}",
            extras={
                "revision": buildinfo.get("revision"),
                "branch": buildinfo.get("branch"),
                "goVersion": buildinfo.get("goVersion"),
                "ready": ready,
                "label_count": label_count,
            },
        )

    async def _ready_flag(self, target: Target) -> bool:
        """Return ``True`` when ``GET /ready`` reports the instance ready."""
        try:
            resp = await self._unauth_get(target, _READY_PATH)
        except (httpx.HTTPError, OSError):
            return False
        return resp.status_code == 200 and "ready" in (resp.text or "").strip().lower()

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability check via the tenant-free ``GET /ready``.

        Loki serves ``/ready`` without an org id or credential, so it is the
        right reachability surface: ``200 "ready"`` -> ok; a ``503`` (ingester
        not ready) or transport error -> not ok with a structured reason.
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)

        def _result(ok: bool, reason: str | None) -> ProbeResult:
            return ProbeResult(
                ok=ok,
                reason=reason,
                latency_ms=(time.monotonic() - start) * 1000.0,
                probed_at=probed_at,
            )

        try:
            resp = await self._unauth_get(target, _READY_PATH)
        except (httpx.HTTPError, OSError) as exc:
            return _result(False, f"{type(exc).__name__}: {exc}")
        if resp.status_code == 200 and "ready" in (resp.text or "").strip().lower():
            return _result(True, None)
        return _result(False, "not_ready")

    # ------------------------------------------------------------------
    # Registration + dispatch shim
    # ------------------------------------------------------------------

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`LOKI_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan (via the registrar queued in
        :mod:`meho_backplane.connectors.loki.__init__`) after the registry has
        eager-imported every connector module. Idempotent across pod restarts,
        mirroring the argocd / bind9 shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        for op in LOKI_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"LokiConnector op {op.op_id!r} declares handler_attr="
                    f"{op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = LOKI_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"LokiConnector op {op.op_id!r} declares group_key="
                        f"{op.group_key!r} but no curated when_to_use exists for that key. "
                        "Add an entry to LOKI_WHEN_TO_USE_BY_GROUP in loki/ops.py."
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
            "loki_operations_registered",
            count=len(LOKI_OPS),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(self, target: Target, op_id: str, params: dict[str, Any]) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Mirrors :meth:`ArgoCdConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs)
        construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly. The connector's
        natural key encodes as ``"loki-api-3.x"`` per ``parse_connector_id``.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:loki-api-connector-shim",
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
