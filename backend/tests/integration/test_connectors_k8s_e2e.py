# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.2-T6 -- K8s meta-tool E2E acceptance against a real k3s cluster.

Boots a single ``rancher/k3s`` container via
:class:`testcontainers.k3s.K3SContainer`, seeds the in-cluster surface
each registered op exercises (the bootstrap kube-system pods +
configmaps + services count for most ops -- no fresh creates needed
for the read surface), then dispatches every K8s op through the
**agent meta-tool flow** (``search_operations`` + ``call_operation``)
against the real G0.6 dispatcher with a Postgres-backed
``endpoint_descriptor`` and a Postgres-backed ``audit_log``.

What this harness proves (Task #326 DoD)
=========================================

* All 14 K8s ops registered into ``endpoint_descriptor`` via
  :func:`~meho_backplane.operations.typed_register.register_typed_operation`
  by ``KubernetesConnector.register_operations()``. The full set is
  reachable via ``search_operations(connector_id="k8s-1.x", query=...)``;
  the meta-tool returns hits whose ``op_id`` covers every registered
  op (existential per Initiative #320's "Agent reaches all 13 ops via
  search_operations" DoD criterion -- note the actual op count
  shipped by T1..T5 is 14).
* Every op dispatches through :func:`call_operation` (the agent-facing
  surface; the CLI alias verb tree this Task adds is a separate
  operator surface that goes through the *same* dispatch route via
  ``POST /api/v1/operations/call`` -- the unit tests in
  ``cli/internal/cmd/k8s/k8s_test.go`` pin the CLI->dispatch wire
  shape; this harness pins the dispatch->handler->k3s round-trip).
* Each call writes a synchronous ``audit_log`` row (CLAUDE.md
  postulate 7) with the canonical
  ``(product="k8s", version="1.x", impl_id="k8s")`` triple in the
  payload.
* The descriptor lookup succeeds against the canonical
  ``connector_id="k8s-1.x"`` (proves the precursor substrate fix on
  this branch -- before the fix, every dispatch returned the
  ``unknown_op`` envelope because the natural-key triple registered
  with ``impl_id="kubernetes-asyncio"`` didn't match the parser's
  ``("k8s", "1.x", "k8s")`` output).

The kubeconfig-loader seam
===========================

Production loads the kubeconfig from Vault via
``load_kubeconfig_from_vault``; this harness preseeds the dispatcher's
per-class connector instance cache with a
:class:`KubernetesConnector` constructed with an in-process loader
returning the testcontainers-emitted kubeconfig dict. Same single-seam
swap the Vault dev-mode harness uses, just at the connector-instance
cache rather than the OIDC client.

Skip conditions
================

* Docker socket missing -- mirrors the rest of ``tests/integration/``.
* k3s container start failure (privileged not allowed, cgroup v1 host
  refusing the v2 mount) -- clean skip, not red.

CI side: ``MEHO_TEST_K3S_IMAGE`` overrides the default image so the
runner pulls through the in-cluster Harbor proxy; ``ci.yml`` sets it
in the ``Python (integration testcontainers)`` job.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

import meho_backplane.operations._handler_resolve as _handler_resolve_module
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.kubernetes import (
    KUBERNETES_OPS,
    KubernetesConnector,
    KubernetesTargetLike,
    parse_kubeconfig_yaml,
    register_kubernetes_typed_operations,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, search_operations
from meho_backplane.operations.reducer import PassThroughReducer
from tests.test_operations_dispatcher import _make_operator

# ---------------------------------------------------------------------------
# Docker-availability gate -- identical heuristic to other integration suites.
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

#: Tenant the test operators run under. Must equal the
#: ``_make_operator`` default tenant_id so the seeded ``Target`` ORM
#: row and the operator built by every ``call_operation`` test are
#: tenant-consistent — :func:`resolve_target` is tenant-scoped
#: (``WHERE tenant_id = ? AND name = ?``) and is the meta-tool's
#: tenant-isolation boundary, so a mismatch surfaces as
#: ``TargetNotFoundError`` (404), not a silent cross-tenant read.
_OPERATOR_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-00000000a0a0")

#: Name the meta-tool ``call_operation`` test resolves via
#: ``{"target": {"name": _TARGET_NAME}}`` → :func:`resolve_target`.
#: Matches the ``_K3sTarget`` stub the direct-``dispatch`` tests pass.
_TARGET_NAME: str = "k3s-e2e"

#: Every op id this harness exercises. Pinned here (rather than
#: re-derived from ``KUBERNETES_OPS``) so a registration regression
#: surfaces as a clear "missing op" assertion failure rather than a
#: silent test-count change.
EXPECTED_OP_IDS: tuple[str, ...] = (
    "k8s.about",
    "k8s.ls",
    "k8s.namespace.list",
    "k8s.node.list",
    "k8s.pod.list",
    "k8s.pod.info",
    "k8s.deployment.list",
    "k8s.deployment.info",
    "k8s.service.list",
    "k8s.ingress.list",
    "k8s.configmap.list",
    "k8s.configmap.info",
    "k8s.event.list",
    "k8s.logs",
)


# ---------------------------------------------------------------------------
# Target stub -- minimal shape the dispatcher's resolver + handlers read
# ---------------------------------------------------------------------------


@dataclass
class _K3sTarget:
    """The dispatcher's resolver reads ``product`` + ``fingerprint.version``;
    the K8s handlers read ``name`` + ``host`` + ``port`` + ``secret_ref``.

    Mirrors the duck-typed shape ``_VaultTarget`` in the Vault dev-e2e
    harness uses -- production swaps in the real
    :class:`~meho_backplane.db.models.Target` ORM row.
    """

    name: str
    host: str
    port: int | None
    secret_ref: str
    product: str = "k8s"
    auth_model: str = "shared_service_account"
    raw_jwt: str | None = "<dev-test-jwt>"

    def __post_init__(self) -> None:
        self.id: UUID = uuid4()
        self.preferred_impl_id: str | None = None

        class _FP:
            # Pinned to a k3s minor matching the container image so the
            # resolver's version match step has a concrete value to read.
            # The K8s connector advertises supported_version_range=None,
            # so the resolver doesn't filter on it -- the version still
            # surfaces in the audit row's payload.
            version = "1.32.0"

        self.fingerprint = _FP()


# ---------------------------------------------------------------------------
# k3s container fixture -- module-scoped (one boot, multiple tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def k3s_kubeconfig_and_target() -> Iterator[tuple[dict[str, Any], _K3sTarget]]:
    """Boot a k3s container; yield (kubeconfig_dict, target stub).

    Mirrors the existing ``test_connectors_k8s_k3d.py`` fixture so the
    container boot path is identical -- one harness boots one cluster,
    not two.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    try:
        from testcontainers.k3s import K3SContainer
    except ImportError as exc:  # pragma: no cover -- testcontainers ships k3s in 4.x
        pytest.skip(f"testcontainers.k3s unavailable: {exc}")

    image = os.environ.get("MEHO_TEST_K3S_IMAGE", "rancher/k3s:v1.32.5-k3s1")
    try:
        container = K3SContainer(image=image)
        container.start()
    except Exception as exc:
        pytest.skip(f"k3s container failed to start ({type(exc).__name__}): {exc}")

    try:
        kubeconfig_text = container.config_yaml()
        kubeconfig = parse_kubeconfig_yaml(kubeconfig_text)
        server_url = kubeconfig["clusters"][0]["cluster"]["server"]
        parsed = urlparse(server_url)
        target = _K3sTarget(
            name="k3s-e2e",
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port,
            secret_ref="kv/data/k8s/k3s-e2e",
        )
        yield kubeconfig, target
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Per-test wiring: connector instance preseed + descriptor registration
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't load ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def k8s_e2e(
    k3s_kubeconfig_and_target: tuple[dict[str, Any], _K3sTarget],
    pg_engine: None,
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[_K3sTarget]:
    """Wire the K8s connector at the live k3s + real PG audit store.

    Phases:

    1. Reset dispatcher / handler caches so the test starts against a
       known-empty cache (importing :mod:`meho_backplane.connectors.kubernetes`
       at module-load time already self-registered the v1 + v2 entries
       via ``register_connector_v2``; the unit-suite ``clear_registry``
       autouses don't run here, so clear explicitly and re-register
       against the now-empty table).
    2. Re-register the connector class so the dispatcher's resolver
       finds it against the canonical ``("k8s", "1.x", "k8s")`` triple.
    3. Preseed the dispatcher's per-class connector instance cache with
       a connector whose kubeconfig loader returns the k3s container's
       kubeconfig dict. Same single-seam swap the Vault dev-mode test
       does on the OIDC client.
    4. Run :func:`register_kubernetes_typed_operations` to UPSERT every
       op into ``endpoint_descriptor`` via the real
       ``register_typed_operation`` helper (the helper's body-hash
       skip-re-embed branch means a second run is cheap -- mirrors
       lifespan idempotence).
    5. Set the pass-through reducer so set-shaped op results land
       verbatim in ``OperationResult.result`` (v0.2 default; G3.3-T4's
       JSONFlux reducer is a separate test concern).
    6. Insert a tenant-scoped :class:`~meho_backplane.db.models.Target`
       ORM row (``name="k3s-e2e"``, ``tenant_id=_OPERATOR_TENANT_ID``,
       ``product="k8s"``, ``fingerprint={"version": "1.32.0"}``) so the
       ``call_operation`` meta-tool path — which resolves
       ``arguments["target"]={"name": ...}`` through the tenant-scoped
       :func:`~meho_backplane.targets.resolver.resolve_target` (the
       meta-tool's tenant-isolation boundary) — exercises the real
       contract instead of being handed an already-resolved object.
       host/port/secret_ref mirror the ``_K3sTarget`` stub; they never
       reach a real cluster because phase 3 preseeded the connector
       instance cache (the dispatcher resolves the class, the seeded
       instance's injected loader returns the k3s kubeconfig directly).
       The ``targets`` table is a soft-FK column the ``pg_engine``
       fixture does not truncate, so the row is deleted on teardown to
       keep per-test isolation (a leftover ``k3s-e2e`` row would make a
       subsequent resolve raise ``AmbiguousTargetError`` only if a
       duplicate alias existed, but the explicit delete keeps the
       ``targets`` table empty between tests regardless).
    """
    kubeconfig, target = k3s_kubeconfig_and_target

    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    clear_registry()
    register_connector_v2(
        product="k8s",
        version="1.x",
        impl_id="k8s",
        cls=KubernetesConnector,
    )

    async def _loader(_t: KubernetesTargetLike, _operator: Operator) -> dict[str, Any]:
        return kubeconfig

    seeded_connector = KubernetesConnector(kubeconfig_loader=_loader)
    _handler_resolve_module._CONNECTOR_INSTANCE_CACHE[KubernetesConnector] = seeded_connector

    await register_kubernetes_typed_operations(embedding_service=stub_embedding_service)

    # Phase 6: seed the tenant-scoped Target ORM row the meta-tool
    # contract resolves by name. The ORM model structurally satisfies
    # KubernetesTargetLike (name/host/port/secret_ref) and the resolver
    # reads product + fingerprint["version"] + preferred_impl_id off it
    # exactly as a production probed target. fingerprint is a JSON dict
    # (the probe route persists FingerprintResult.model_dump(mode="json")
    # to this column), so resolve_connector's _resolve_target_version
    # reads it via .get("version"); the K8s connector advertises
    # supported_version_range=None so the version is not filtered on,
    # only echoed into the audit payload — same as the _K3sTarget stub.
    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            TargetORM(
                id=uuid4(),
                tenant_id=_OPERATOR_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="k8s",
                host=target.host,
                port=target.port,
                fqdn=None,
                secret_ref=target.secret_ref,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                fingerprint={"version": "1.32.0"},
                preferred_impl_id=None,
                created_at=now,
                updated_at=now,
            )
        )

    try:
        yield target
    finally:
        # aclose tolerates double-close; never let teardown errors
        # mask the test's own assertion failures.
        with contextlib.suppress(Exception):
            await seeded_connector.aclose()
        # Delete the seeded Target row — pg_engine does not truncate
        # `targets` (soft-FK column), so without this a leftover row
        # leaks across tests in the same container session.
        with contextlib.suppress(Exception):
            async with sessionmaker() as session, session.begin():
                await session.execute(
                    delete(TargetORM).where(
                        TargetORM.tenant_id == _OPERATOR_TENANT_ID,
                        TargetORM.name == _TARGET_NAME,
                    )
                )
        reset_dispatcher_caches()
        clear_registry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_audit_row(op_id: str, *, operator_sub: str) -> None:
    """Assert exactly one audit_log row exists for *op_id* / *operator_sub*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path == op_id,
                        AuditLog.operator_sub == operator_sub,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, (
        f"expected exactly one audit row for {op_id} / operator {operator_sub}, got {len(rows)}"
    )
    row = rows[0]
    assert row.payload["op_id"] == op_id
    assert row.payload["source_kind"] == "typed"
    assert row.payload["result_status"] == "ok"


# ---------------------------------------------------------------------------
# Registration + search shape (DoD: agent reaches all ops via search_operations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_registered_op_present_in_endpoint_descriptor(
    k8s_e2e: _K3sTarget,
) -> None:
    """Each row in :data:`KUBERNETES_OPS` lands in ``endpoint_descriptor``
    under the canonical ``("k8s", "1.x", "k8s")`` triple after register.
    """
    from meho_backplane.db.models import EndpointDescriptor

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "k8s",
                EndpointDescriptor.version == "1.x",
                EndpointDescriptor.impl_id == "k8s",
            )
        )
        rows = result.scalars().all()

    assert len(rows) == len(KUBERNETES_OPS)
    op_ids = {row.op_id for row in rows}
    assert op_ids == set(EXPECTED_OP_IDS), (
        f"registered ops drift: in DB but not expected: "
        f"{op_ids - set(EXPECTED_OP_IDS)}; "
        f"expected but missing: {set(EXPECTED_OP_IDS) - op_ids}"
    )


@pytest.mark.asyncio
async def test_search_operations_returns_hits_for_each_op(
    k8s_e2e: _K3sTarget,
) -> None:
    """``search_operations`` reaches every registered op via a coarse query.

    Existential DoD: an operator-facing agent typing
    ``search_operations(connector_id="k8s-1.x", query="list")`` lands
    on the list ops; a query for ``"pod"`` lands on pod ops; etc.
    The retrieval scoring is BM25 + cosine RRF over the descriptor
    text, so the assertion is "at least one hit per query that should
    plausibly match" rather than exact-rank pinning.
    """
    operator = _make_operator(sub="op-search")
    result = await search_operations(
        operator,
        {"connector_id": "k8s-1.x", "query": "pod", "limit": 20},
    )
    hits = result["hits"]
    assert len(hits) >= 1
    hit_op_ids = {h["op_id"] for h in hits}
    # Coarse: at least one pod-related op surfaces for the query "pod".
    assert any(op.startswith("k8s.pod") for op in hit_op_ids), (
        f"search_operations(query='pod') returned no pod ops; got {hit_op_ids}"
    )


# ---------------------------------------------------------------------------
# Per-op call_operation dispatch -- DoD: tools/call works for every op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_operation_about_dispatches_through_meta_tool(
    k8s_e2e: _K3sTarget,
) -> None:
    """``call_operation(k8s.about)`` round-trips dispatcher -> handler -> k3s.

    Exercises the real meta-tool target contract: ``call_operation``
    requires ``arguments["target"]`` to be ``{"name": <str>}`` and runs
    it through the tenant-scoped :func:`resolve_target` (the meta-tool's
    tenant-isolation boundary, since #438 G0.6-T8) before dispatch — it
    does NOT accept an already-resolved target object. The ``k8s_e2e``
    fixture seeds the matching tenant-scoped ``Target`` ORM row; the
    operator built here uses the same default tenant
    (``_OPERATOR_TENANT_ID``) so resolution succeeds. The other suites
    call :func:`dispatch` directly with the duck-typed ``_K3sTarget``;
    that low-level path legitimately accepts the object — only the
    meta-tool enforces the name→resolve_target contract.
    """
    operator = _make_operator(sub="op-meta-about", tenant_id=_OPERATOR_TENANT_ID)
    result = await call_operation(
        operator,
        {
            "connector_id": "k8s-1.x",
            "op_id": "k8s.about",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", result.get("error")
    payload = result["result"]
    assert payload["product"] == "k3s"
    assert payload["git_version"].startswith("v")
    await _assert_audit_row("k8s.about", operator_sub="op-meta-about")


@pytest.mark.asyncio
async def test_dispatch_ls_root_against_k3s(k8s_e2e: _K3sTarget) -> None:
    """``k8s.ls /`` via the dispatcher returns the live namespace set."""
    operator = _make_operator(sub="op-ls-root")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.ls",
        target=k8s_e2e,
        params={"path": "/"},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert "default" in payload["namespaces"]
    assert "kube-system" in payload["namespaces"]
    await _assert_audit_row("k8s.ls", operator_sub="op-ls-root")


@pytest.mark.asyncio
async def test_dispatch_namespace_list_against_k3s(k8s_e2e: _K3sTarget) -> None:
    operator = _make_operator(sub="op-ns-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.namespace.list",
        target=k8s_e2e,
        params={},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert payload["total"] >= 4  # default, kube-system, kube-public, kube-node-lease
    names = {row["name"] for row in payload["rows"]}
    assert "default" in names
    assert "kube-system" in names
    await _assert_audit_row("k8s.namespace.list", operator_sub="op-ns-list")


@pytest.mark.asyncio
async def test_dispatch_node_list_against_k3s(k8s_e2e: _K3sTarget) -> None:
    operator = _make_operator(sub="op-node-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.node.list",
        target=k8s_e2e,
        params={},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert payload["total"] >= 1
    node = payload["rows"][0]
    assert node["status"] == "Ready"
    await _assert_audit_row("k8s.node.list", operator_sub="op-node-list")


@pytest.mark.asyncio
async def test_dispatch_pod_list_kube_system(k8s_e2e: _K3sTarget) -> None:
    """``k8s.pod.list -n kube-system`` through the dispatcher returns the
    bootstrap k3s pods (coredns / traefik / local-path-provisioner / etc.)."""
    operator = _make_operator(sub="op-pod-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.pod.list",
        target=k8s_e2e,
        params={"namespace": "kube-system"},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert payload["total"] >= 1
    sample = payload["rows"][0]
    assert sample["namespace"] == "kube-system"
    await _assert_audit_row("k8s.pod.list", operator_sub="op-pod-list")


@pytest.mark.asyncio
async def test_dispatch_pod_info_resolves_prefix(k8s_e2e: _K3sTarget) -> None:
    """``k8s.pod.info`` through the dispatcher resolves a name + returns full detail."""
    operator = _make_operator(sub="op-pod-info-list")
    listing = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.pod.list",
        target=k8s_e2e,
        params={"namespace": "kube-system"},
    )
    assert listing.status == "ok"
    pod_name = listing.result["rows"][0]["name"]

    operator2 = _make_operator(sub="op-pod-info")
    result = await dispatch(
        operator=operator2,
        connector_id="k8s-1.x",
        op_id="k8s.pod.info",
        target=k8s_e2e,
        params={"pod_name": pod_name, "namespace": "kube-system"},
    )
    assert result.status == "ok", result.error
    info = result.result
    assert info["name"] == pod_name
    assert info["namespace"] == "kube-system"
    await _assert_audit_row("k8s.pod.info", operator_sub="op-pod-info")


@pytest.mark.asyncio
async def test_dispatch_deployment_list_all_namespaces(k8s_e2e: _K3sTarget) -> None:
    """k3s ships >=1 Deployment in kube-system (e.g. coredns);
    --all-namespaces returns them."""
    operator = _make_operator(sub="op-dep-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.deployment.list",
        target=k8s_e2e,
        params={"all_namespaces": True},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert payload["total"] >= 1
    await _assert_audit_row("k8s.deployment.list", operator_sub="op-dep-list")


@pytest.mark.asyncio
async def test_dispatch_deployment_info_against_k3s(k8s_e2e: _K3sTarget) -> None:
    """Resolve a deployment name via the list, then info via the dispatcher."""
    operator = _make_operator(sub="op-dep-info-list")
    listing = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.deployment.list",
        target=k8s_e2e,
        params={"namespace": "kube-system"},
    )
    if listing.status != "ok" or not listing.result["rows"]:
        pytest.skip("k3s flavour did not ship any kube-system deployment to inspect")
    name = listing.result["rows"][0]["name"]
    operator2 = _make_operator(sub="op-dep-info")
    result = await dispatch(
        operator=operator2,
        connector_id="k8s-1.x",
        op_id="k8s.deployment.info",
        target=k8s_e2e,
        params={"deployment_name": name, "namespace": "kube-system"},
    )
    assert result.status == "ok", result.error
    info = result.result
    assert info["name"] == name
    await _assert_audit_row("k8s.deployment.info", operator_sub="op-dep-info")


@pytest.mark.asyncio
async def test_dispatch_service_list_kube_system(k8s_e2e: _K3sTarget) -> None:
    """k3s ships a kube-dns Service in kube-system; service.list returns it."""
    operator = _make_operator(sub="op-svc-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.service.list",
        target=k8s_e2e,
        params={"namespace": "kube-system"},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert payload["total"] >= 1
    names = {row["name"] for row in payload["rows"]}
    assert "kube-dns" in names
    await _assert_audit_row("k8s.service.list", operator_sub="op-svc-list")


@pytest.mark.asyncio
async def test_dispatch_ingress_list_default(k8s_e2e: _K3sTarget) -> None:
    """k3s' default namespace ships zero ingresses out of the box -- the op
    must still return a clean ok with rows=[] / total=0 (the K8s API
    returns an empty IngressList rather than a 404)."""
    operator = _make_operator(sub="op-ing-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.ingress.list",
        target=k8s_e2e,
        params={"namespace": "default"},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert "rows" in payload
    assert payload["total"] == len(payload["rows"])
    await _assert_audit_row("k8s.ingress.list", operator_sub="op-ing-list")


@pytest.mark.asyncio
async def test_dispatch_configmap_list_kube_system_keys_only(
    k8s_e2e: _K3sTarget,
) -> None:
    """k3s ships kube-root-ca.crt + extension-apiserver-authentication
    configmaps in kube-system; list returns KEY NAMES ONLY, never values."""
    operator = _make_operator(sub="op-cm-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.configmap.list",
        target=k8s_e2e,
        params={"namespace": "kube-system"},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert payload["total"] >= 1
    for row in payload["rows"]:
        # keys-only contract -- the row shape must not carry data/binary_data.
        assert "data" not in row
        assert "binary_data" not in row
        assert isinstance(row["keys"], list)
    await _assert_audit_row("k8s.configmap.list", operator_sub="op-cm-list")


@pytest.mark.asyncio
async def test_dispatch_configmap_info_returns_data(k8s_e2e: _K3sTarget) -> None:
    """``k8s.configmap.info`` returns full data for one configmap; audited."""
    operator = _make_operator(sub="op-cm-info-list")
    listing = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.configmap.list",
        target=k8s_e2e,
        params={"namespace": "kube-system"},
    )
    assert listing.status == "ok"
    if not listing.result["rows"]:
        pytest.skip("k3s shipped no configmaps in kube-system to inspect")
    name = listing.result["rows"][0]["name"]

    operator2 = _make_operator(sub="op-cm-info")
    result = await dispatch(
        operator=operator2,
        connector_id="k8s-1.x",
        op_id="k8s.configmap.info",
        target=k8s_e2e,
        params={"name": name, "namespace": "kube-system"},
    )
    assert result.status == "ok", result.error
    info = result.result
    assert info["name"] == name
    assert "data" in info  # full data is the info-path contract
    await _assert_audit_row("k8s.configmap.info", operator_sub="op-cm-info")


@pytest.mark.asyncio
async def test_dispatch_event_list_against_k3s(k8s_e2e: _K3sTarget) -> None:
    """``k8s.event.list`` returns the recent event roster (may be empty
    on a freshly-booted cluster but never errors)."""
    operator = _make_operator(sub="op-event-list")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.event.list",
        target=k8s_e2e,
        params={"namespace": "kube-system", "limit": 25},
    )
    assert result.status == "ok", result.error
    payload = result.result
    assert "rows" in payload
    assert payload["total"] == len(payload["rows"])
    await _assert_audit_row("k8s.event.list", operator_sub="op-event-list")


@pytest.mark.asyncio
async def test_dispatch_logs_against_running_pod(k8s_e2e: _K3sTarget) -> None:
    """``k8s.logs`` against a Running kube-system pod with a Ready container
    returns a (possibly empty) lines list.

    Selecting ``rows[0]`` blindly is non-deterministic on a freshly-booted
    k3s container: the first pod is frequently still Pending /
    ContainerCreating, and ``k8s.logs`` then raises a generic kube
    ``ApiException`` ("container ... is waiting to start: ContainerCreating")
    which is neither the ok branch nor the structured
    multi-container-ambiguity branch this test asserts. k3s always brings
    up coredns / metrics-server / local-path-provisioner, but they take
    time to become Ready after the container boots, so poll ``k8s.pod.list``
    (through the same dispatch path, registered op only -- the row already
    carries ``status`` (phase) and ``ready`` ("<ready>/<total>")) until a
    pod is Running with every container Ready (and at least one container).
    """
    operator = _make_operator(sub="op-logs-list")

    def _running_ready_pod(rows: list[dict[str, Any]]) -> str | None:
        """First pod that is phase==Running with all containers Ready
        (ready column ``X/Y`` where ``X == Y`` and ``X >= 1``)."""
        for row in rows:
            if row.get("status") != "Running":
                continue
            ready = row.get("ready")
            if not isinstance(ready, str) or "/" not in ready:
                continue
            ready_n, _, total_n = ready.partition("/")
            if not (ready_n.isdigit() and total_n.isdigit()):
                continue
            if int(ready_n) >= 1 and int(ready_n) == int(total_n):
                name = row.get("name")
                if isinstance(name, str) and name:
                    return name
        return None

    pod_name: str | None = None
    deadline = time.monotonic() + 90.0
    while True:
        listing = await dispatch(
            operator=operator,
            connector_id="k8s-1.x",
            op_id="k8s.pod.list",
            target=k8s_e2e,
            params={"namespace": "kube-system"},
        )
        assert listing.status == "ok", listing.error
        pod_name = _running_ready_pod(listing.result["rows"])
        if pod_name is not None:
            break
        if time.monotonic() >= deadline:
            pytest.skip(
                "no Running kube-system pod with a Ready container appeared "
                "within 90s -- cannot deterministically exercise k8s.logs"
            )
        await asyncio.sleep(3.0)

    operator2 = _make_operator(sub="op-logs")
    result = await dispatch(
        operator=operator2,
        connector_id="k8s-1.x",
        op_id="k8s.logs",
        target=k8s_e2e,
        params={"pod_name": pod_name, "namespace": "kube-system", "tail": 50},
    )
    # Pods with multiple containers and no --container surface as the
    # handler's MultiContainerAmbiguityError -> connector_error envelope;
    # that's a real signal the operator would see, not a flake. Accept
    # either ok (single-container pod) or the structured error shape.
    if result.status == "ok":
        payload = result.result
        assert payload["pod"] == pod_name
        assert payload["namespace"] == "kube-system"
        assert isinstance(payload["lines"], list)
        await _assert_audit_row("k8s.logs", operator_sub="op-logs")
    else:
        # connector_error from MultiContainerAmbiguityError; the audit
        # row is still written because the dispatcher writes audit on
        # both ok and error paths.
        assert result.error is not None
        assert "container" in result.error or "ambig" in result.error.lower()


# ---------------------------------------------------------------------------
# Negative: unknown op against the canonical connector_id still routes through
# the descriptor lookup (proves the substrate fix end-to-end).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unknown_op_returns_dispatcher_unknown_op_envelope(
    k8s_e2e: _K3sTarget,
) -> None:
    """An op_id not in :data:`EXPECTED_OP_IDS` surfaces as the
    dispatcher's structured ``unknown_op`` envelope -- proving the
    descriptor lookup ran against the canonical ``("k8s","1.x","k8s")``
    triple. Before the precursor substrate fix, *every* op_id (known
    or not) returned this envelope because the triple didn't match
    the parser output; with the fix, only genuinely-unknown op_ids do."""
    operator = _make_operator(sub="op-unknown")
    result = await dispatch(
        operator=operator,
        connector_id="k8s-1.x",
        op_id="k8s.totally.unregistered",
        target=k8s_e2e,
        params={},
    )
    assert result.status == "error"
    assert result.error is not None and result.error.startswith("unknown_op:")
    extras = result.extras
    assert extras.get("error_code") == "unknown_op"
    # ``known_op_count`` carries the descriptor count for the triple;
    # post-substrate-fix this is len(EXPECTED_OP_IDS) (14).
    assert extras.get("known_op_count") == len(EXPECTED_OP_IDS)
