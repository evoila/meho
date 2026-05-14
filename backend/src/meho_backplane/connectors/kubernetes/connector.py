# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""KubernetesConnector -- fingerprint / probe / dispatcher-shim.

The G3.2-T1 (#321) skeleton wired ``fingerprint`` + ``probe`` + a stub
``execute`` that returned ``unknown_op`` for every op_id. The G0.6
refactor (#391) keeps the fingerprint + probe paths byte-for-byte
unchanged and refactors ``execute`` into a thin shim that delegates to
the G0.6 dispatcher substrate:

* :attr:`KubernetesConnector.version` /
  :attr:`KubernetesConnector.impl_id` advertise the registry v2 key
  ``("k8s", "1.x", "kubernetes-asyncio")``. The shipped v1 entry
  (``register_connector("k8s", ...)``) is retained for
  ``get_connector("k8s")`` callers (resolver tests, ``/api/v1/health``
  Vault federation probe shape). The chassis dispatch route that
  originally motivated it (``POST /api/v1/connectors/{product}/{op_id}``)
  was deprecated and removed by G0.6-T11 (#412); the canonical
  dispatch surface is now ``POST /api/v1/operations/call``.
* :meth:`register_operations` is a classmethod called from the
  application lifespan. It walks
  :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS` and
  upserts each row into ``endpoint_descriptor`` via
  :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
  Idempotent (the helper's body-hash skip-re-embed branch keeps pod
  restarts cheap).
* :meth:`about` is the canary op the refactor registers against the
  new substrate. The handler reuses the same
  ``kubernetes_asyncio.client.VersionApi.get_code`` call
  :meth:`fingerprint` already issues, returning a flat dict the
  dispatcher's reducer wraps into ``OperationResult.result``.
* :meth:`execute` shims into the dispatcher's lookup + handler-resolve
  + invoke path so unknown op_ids return the same
  ``OperationResult(status="error", error="unknown_op: ...")`` shape
  the dispatcher emits everywhere else. The operator-aware path is
  ``call_operation`` / ``/api/v1/operations/call`` via the G0.6
  meta-tools; :meth:`execute` remains the typed-connector entry the
  dispatcher invokes for ``source_kind == "typed"`` rows.

The skeleton's per-target :class:`kubernetes_asyncio.client.ApiClient`
cache, the asyncio-lock protecting it, and :meth:`aclose` are all
preserved verbatim.

Product flavour (``"rke2"`` / ``"k3s"`` / ``"eks"`` / ``"gke"`` /
``"aks"`` / ``"vanilla"``) is derived from the ``gitVersion`` suffix
returned by the API server -- sufficient for v0.2's version-tagged
doc/kb lookup and broadcast classifier without an extra round-trip.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from kubernetes_asyncio import client, config

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.kubernetes.kubeconfig import (
    KubeconfigLoader,
    KubernetesTargetLike,
    load_kubeconfig_from_vault,
)
from meho_backplane.connectors.kubernetes.ops import KUBERNETES_OPS
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["KubernetesConnector", "product_from_git_version"]

_log = structlog.get_logger(__name__)

_DEFAULT_K8S_PORT = 6443
_PROBE_TIMEOUT_SECONDS = 5.0
_PROBE_OK_STATUSES = frozenset({200, 401})


def product_from_git_version(git_version: str) -> str:
    """Map a Kubernetes ``gitVersion`` string to a product slug.

    The k8s API server's ``/version`` endpoint returns ``gitVersion`` in
    the form ``v<major>.<minor>.<patch><suffix>``. The suffix encodes the
    distribution: ``+rke2r1`` for RKE2, ``+k3s1`` for K3s, ``-eks-…`` for
    EKS, ``-gke.…`` for GKE, ``-aks`` for AKS. Vanilla upstream has no
    suffix (or ``+0`` for some custom builds).
    """
    if "+rke2" in git_version:
        return "rke2"
    if "+k3s" in git_version:
        return "k3s"
    if "-eks-" in git_version:
        return "eks"
    if "-gke." in git_version:
        return "gke"
    if "-aks" in git_version:
        return "aks"
    return "vanilla"


class KubernetesConnector(Connector):
    """Kubernetes connector -- reads kubeconfig per target, caches the client."""

    # Registry v2 metadata (G0.6-T3 #394 + #391 refactor). The product
    # slug ``"k8s"`` is the v2 canonical form aligned with the
    # ``connector_id="k8s-1.x"`` shape the dispatcher's parser produces
    # (:func:`~meho_backplane.operations._lookup.parse_connector_id`).
    # The v1 entry registered in :mod:`__init__` uses ``"kubernetes"``
    # for chassis-route backward compat -- both keys resolve to the
    # same connector class via the registry's two-layer storage.
    product = "k8s"
    version = "1.x"
    impl_id = "kubernetes-asyncio"

    def __init__(
        self,
        *,
        kubeconfig_loader: KubeconfigLoader | None = None,
    ) -> None:
        self._kubeconfig_loader: KubeconfigLoader = (
            kubeconfig_loader if kubeconfig_loader is not None else load_kubeconfig_from_vault
        )
        self._api_clients: dict[str, client.ApiClient] = {}
        self._lock = asyncio.Lock()

    async def fingerprint(self, target: KubernetesTargetLike) -> FingerprintResult:
        """Canonical fingerprint built from ``VersionApi.get_code()``."""
        api_client = await self._get_api_client(target)
        version_api = client.VersionApi(api_client)
        version = await version_api.get_code()
        return FingerprintResult(
            vendor="kubernetes",
            product=product_from_git_version(version.git_version),
            version=version.git_version,
            build=version.build_date,
            edition=None,
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="GET /version",
            extras={
                "major": version.major,
                "minor": version.minor,
                "platform": version.platform,
                "go_version": version.go_version,
                "git_commit": version.git_commit,
                "git_tree_state": version.git_tree_state,
            },
        )

    async def probe(self, target: KubernetesTargetLike) -> ProbeResult:
        """Kubeconfig-free reachability check against ``/readyz`` (or ``/healthz``).

        TLS verification is intentionally disabled (NOSONAR S4830): the
        probe is a reachability signal, not an auth check, and runs
        before any kubeconfig is loaded, so the CA bundle is not yet
        known. A 401 response is treated as success — it means the API
        server is up and speaking TLS; auth surfaces at :meth:`execute`
        time. Real certificate validation happens via the kubeconfig's
        ``certificate-authority-data`` once the operator's identity is
        in play.

        Endpoint fallback: ``GET /readyz`` first; on HTTP 404 retry
        ``GET /healthz`` (legacy clusters that predate ``/readyz`` or
        have it disabled). The first response whose status is in
        :data:`_PROBE_OK_STATUSES` short-circuits the probe.
        """
        port = target.port if target.port is not None else _DEFAULT_K8S_PORT
        base_url = f"https://{target.host}:{port}"
        start = time.monotonic()
        probed_at = datetime.now(UTC)
        endpoint = "/readyz"
        try:
            async with httpx.AsyncClient(
                verify=False,  # NOSONAR S4830 — kubeconfig-free reachability probe; see docstring
                timeout=_PROBE_TIMEOUT_SECONDS,
            ) as http:
                resp = await http.get(f"{base_url}{endpoint}")
                if resp.status_code == 404:
                    endpoint = "/healthz"
                    resp = await http.get(f"{base_url}{endpoint}")
        except (httpx.HTTPError, OSError) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                latency_ms=None,
                probed_at=probed_at,
            )
        latency_ms = (time.monotonic() - start) * 1000.0
        if resp.status_code in _PROBE_OK_STATUSES:
            return ProbeResult(ok=True, latency_ms=latency_ms, probed_at=probed_at)
        return ProbeResult(
            ok=False,
            reason=f"HTTP {resp.status_code} on {endpoint}",
            latency_ms=latency_ms,
            probed_at=probed_at,
        )

    async def about(
        self,
        target: KubernetesTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Return product flavour + version snapshot for *target*.

        Op-id: ``k8s.about``. The dispatcher routes here after the JSON
        Schema validator has accepted ``params`` (declared empty in
        :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS`)
        and the reducer wraps the returned dict into
        ``OperationResult.result``. The handler reuses the same
        :meth:`kubernetes_asyncio.client.VersionApi.get_code` call
        :meth:`fingerprint` issues so the cluster pays one round-trip per
        ``k8s.about`` dispatch regardless of how many other ops touch
        ``VersionApi``.

        The returned dict is intentionally flat -- no nested
        ``extras`` -- because the dispatcher's
        :class:`~meho_backplane.operations.reducer.PassThroughReducer`
        forwards the value verbatim. Future reducers (real JSONFlux
        reduction in a follow-on Initiative) flatten nested shapes
        anyway; staying flat now means the v0.2 callers see the same
        keys before and after the reducer swap.
        """
        del params  # declared in schema; the handler intentionally ignores them
        api_client = await self._get_api_client(target)
        version_api = client.VersionApi(api_client)
        version = await version_api.get_code()
        return {
            "product": product_from_git_version(version.git_version),
            "git_version": version.git_version,
            "build_date": version.build_date,
            "major": version.major,
            "minor": version.minor,
            "platform": version.platform,
            "go_version": version.go_version,
            "git_commit": version.git_commit,
            "git_tree_state": version.git_tree_state,
        }

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`KUBERNETES_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.kubernetes.ops.KUBERNETES_OPS`
        and routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`,
        which:

        * Derives ``handler_ref`` from the bound method's
          ``__module__`` + ``__qualname__`` (e.g.
          ``"meho_backplane.connectors.kubernetes.connector.KubernetesConnector.about"``).
        * Inserts a new row on first call; skips the embedding compute
          on re-call with unchanged summary / description / tags
          (body-hash skip-re-embed branch).
        * Always advances ``updated_at`` so operators can grep the
          last-registration timestamp.

        Idempotent across pod restarts. Errors propagate to the
        lifespan; the fail-fast deployment shape the rest of the
        chassis tasks established is what the operator wants here
        (a missing migration or a partial DB state is a deploy bug,
        not a runtime degradation).
        """
        # Imported lazily so a test that imports the connector module
        # without the operations package available (e.g. an isolated
        # unit test of ``product_from_git_version``) still works -- the
        # operations package transitively imports the embedding service,
        # which pulls in ONNX runtime and a 100 MB+ model on first
        # touch.
        from meho_backplane.operations.typed_register import register_typed_operation

        # Bind handler attrs once into a list of (op, bound-method)
        # tuples so the error message names the op_id when a typo in
        # KUBERNETES_OPS' handler_attr would otherwise surface as a
        # confusing ``AttributeError`` deep inside the helper.
        bindings: list[tuple[Any, Any]] = []
        for op in KUBERNETES_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"KubernetesConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
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
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "kubernetes_operations_registered",
            count=len(bindings),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    # code-quality-allow: pre-existing G3.2-T1 #321 skeleton; T11 #412
    # only edits the docstring (function is shorter post-edit). Refactor
    # into helpers is deferred to a separate Task.
    async def execute(
        self,
        target: KubernetesTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Dispatcher shim -- delegates to G0.6's ``dispatch``-shaped lookup.

        Routes for *op_id* by:

        1. Looking up the descriptor for
           ``(product=cls.product, version=cls.version, impl_id=cls.impl_id, op_id)``
           against the global / built-in row set
           (``tenant_id IS NULL`` -- typed registrations are always
           global by construction).
        2. Unknown op_id -> the structured ``unknown_op``
           :class:`OperationResult` the dispatcher itself produces, via
           :func:`~meho_backplane.operations._errors.result_unknown_op`.
        3. Known op_id -> resolves ``descriptor.handler_ref`` via
           :func:`~meho_backplane.operations._handler_resolve.import_handler`,
           binds it against this instance when the resolved symbol is
           an unbound method (the bound-method case is the typed-
           connector convention), and invokes it with
           ``(target, params)``.

        The shim is intentionally **operator-less** -- direct callers
        (typed-connector internals, composite handlers) don't carry an
        :class:`~meho_backplane.auth.operator.Operator`, so the full
        dispatcher path (policy gate, audit, broadcast) doesn't run
        from this entry point. The operator-aware surface is
        ``POST /api/v1/operations/call`` via the G0.6 meta-tools; the
        pre-G0.6 chassis route was removed by G0.6-T11 (#412). Within
        the operator-less constraint the shim's contract is:

        * Same ``unknown_op`` shape the dispatcher emits.
        * Same ``connector_error`` shape on handler exceptions.
        * Same ``invalid_params`` shape on schema-validation failures.

        Result envelope mirrors :class:`OperationResult` so the
        FastAPI route's ``unknown_op``-extraction logic continues to
        work unchanged.
        """
        # Lazy imports for the same rationale documented on
        # ``register_operations`` -- pure-python tests that exercise
        # ``fingerprint``/``probe`` shouldn't pay the operations
        # package's import cost.
        from sqlalchemy import select

        from meho_backplane.db.engine import get_sessionmaker
        from meho_backplane.db.models import EndpointDescriptor
        from meho_backplane.operations._errors import (
            result_connector_error,
            result_invalid_params,
            result_unknown_op,
        )
        from meho_backplane.operations._handler_resolve import (
            import_handler,
            is_unbound_method,
        )
        from meho_backplane.operations._lookup import count_known_ops
        from meho_backplane.operations._validate import validate_params

        start = time.monotonic()

        def _elapsed() -> float:
            return (time.monotonic() - start) * 1000.0

        # Global-only descriptor lookup. The dispatcher's
        # ``lookup_descriptor`` takes an operator tenant_id for the
        # tenant-scoped-first fallback; the chassis path lacks one, so
        # we hit only the global row set. Typed registrations are
        # always global (``tenant_id IS NULL``) by construction.
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.product == self.product,
                    EndpointDescriptor.version == self.version,
                    EndpointDescriptor.impl_id == self.impl_id,
                    EndpointDescriptor.op_id == op_id,
                    EndpointDescriptor.is_enabled.is_(True),
                )
            )
            descriptor = result.scalar_one_or_none()

        if descriptor is None:
            known_op_count = await count_known_ops(
                product=self.product,
                version=self.version,
                impl_id=self.impl_id,
            )
            return result_unknown_op(op_id, known_op_count, _elapsed())

        # Parameter validation. The dispatcher runs this before the
        # policy gate; the shim runs it before invocation for the
        # same reason (cheap rejection of malformed inputs).
        validation_errors = validate_params(descriptor.parameter_schema, params)
        if validation_errors:
            return result_invalid_params(op_id, validation_errors, _elapsed())

        # Handler resolution + bound-method binding. ``import_handler``
        # walks the dotted path via importlib + getattr; the bound-
        # method shape (``module.ClassName.method``) returns the
        # unbound function, which we rebind against ``self``.
        handler = import_handler(descriptor.handler_ref or "")
        if is_unbound_method(handler, type(self)):
            handler = handler.__get__(self, type(self))

        try:
            raw = await handler(target=target, params=params)
        except Exception as exc:
            return result_connector_error(op_id, exc, _elapsed())

        return OperationResult(
            status="ok",
            op_id=op_id,
            result=raw if isinstance(raw, (dict, list)) else {"value": raw},
            duration_ms=_elapsed(),
        )

    async def aclose(self) -> None:
        """Close every cached :class:`ApiClient`. Idempotent."""
        async with self._lock:
            for api_client in self._api_clients.values():
                await api_client.close()
            self._api_clients.clear()

    @staticmethod
    def _cache_key(target: KubernetesTargetLike) -> str:
        """Globally unique cache key for *target*.

        Keyed on ``secret_ref`` (the Vault path the kubeconfig lives
        at) rather than ``target.name``. Once G0.3 (#224) lands its
        ``Target`` model, target names are unique only within a tenant
        — two tenants legitimately holding a target both named
        ``"rke2-meho"`` would otherwise share an :class:`ApiClient`
        built from whichever kubeconfig loaded first, and the second
        tenant's ops would silently execute against the first
        tenant's cluster. The Vault path is the operator's chosen
        opaque identifier for the kubeconfig and is globally unique
        by the consumer's ``targets.yaml`` convention. Swap to
        ``target.id`` when G0.3 finalises a row-PK shape.
        """
        return target.secret_ref

    async def _get_api_client(self, target: KubernetesTargetLike) -> client.ApiClient:
        """Resolve (and cache) the :class:`ApiClient` for *target*.

        The single lock serialises concurrent first-use for any target;
        in practice the second caller hits the cache fast-path. The
        slow kubeconfig read happens under the lock so two concurrent
        callers for the same target don't both pay the cost.
        """
        key = self._cache_key(target)
        async with self._lock:
            cached = self._api_clients.get(key)
            if cached is not None:
                return cached
            kubeconfig_dict = await self._kubeconfig_loader(target)
            api_client = await config.new_client_from_config_dict(kubeconfig_dict)
            self._api_clients[key] = api_client
            _log.info(
                "kubernetes_api_client_built",
                target=target.name,
                host=target.host,
            )
            return api_client
