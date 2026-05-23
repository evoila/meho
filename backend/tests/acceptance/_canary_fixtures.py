# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.1-T8 vmware-rest dispatch tests.

Three vcsim-backed acceptance modules (dispatch smoke / JSONFlux force-mode
/ agent-flow E2E) need overlapping setup: a registered
:class:`~meho_backplane.connectors.vmware_rest.VmwareRestConnector`
instance patched for vcsim's no-auth surface, a Target row, and a
small set of dispatchable :class:`EndpointDescriptor` rows. Extracting
that shape into one shared module keeps each test module focused on
its assertions.

Why a minimal direct-insert path (not the full canary ingest)
=============================================================

The G0.7 canary's :func:`ingested_canary` fixture in
``test_g07_vsphere_canary.py`` drives the full
:class:`IngestionPipelineService` end-to-end -- LLM grouping pass,
fastembed embeddings, the auto-shim registration -- against the
~1,275-op ``vcenter.yaml`` spec. That's the right primitive for
search-quality assertions (the canary's govc-parity benchmark) but
overkill for dispatch tests that only need to prove "op_id X routes
through the dispatcher and returns ok against vcsim".

This module inserts only the descriptor rows each dispatch test
actually probes, with hand-authored ``method`` / ``path`` /
``handler_ref`` triples; no LLM, no embeddings, no grouping pass.

All three vcsim dispatch modules consume :func:`ingested_canary_vcsim`
directly, including
:mod:`tests.acceptance.test_vmware_rest_agent_flow_e2e`: its
:func:`~meho_backplane.operations.meta_tools.search_operations` step
ranks over the seeded :data:`DISPATCH_DESCRIPTORS` summary strings on
BM25 alone (the minimal six-op set is enough to rank
``GET:/vcenter/vm`` first for "list virtual machines"; the full-corpus
search-quality assertion stays in ``test_g07_vsphere_canary.py``). All
paths converge on the same patched :class:`VmwareRestConnector`
instance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

__all__ = [
    "CANARY_BASE_URL",
    "CANARY_CONNECTOR_ID",
    "CANARY_FINGERPRINT",
    "CANARY_IMPL_ID",
    "CANARY_OPERATOR_TENANT",
    "CANARY_PRODUCT",
    "CANARY_VERSION",
    "CANARY_VMS",
    "DISPATCH_DESCRIPTORS",
    "VCSIM_TARGET_NAME",
    "IngestedCanaryVcsim",
    "ingested_canary_vcsim",
    "prewarmed_embeddings",
]

#: Connector triple under test. Matches the canary's constants in
#: ``test_g07_vsphere_canary.py`` verbatim — they refer to the same
#: connector and the same descriptor rows.
CANARY_PRODUCT: str = "vmware"
CANARY_VERSION: str = "9.0"
CANARY_IMPL_ID: str = "vmware-rest"
CANARY_CONNECTOR_ID: str = f"{CANARY_IMPL_ID}-{CANARY_VERSION}"

#: Tenant the acceptance operator belongs to. Connector + descriptor
#: rows are tenant-scope NULL (built-in) so the operator's tenant_id
#: only affects audit attribution.
CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000ff")

#: Name used for the seeded vcsim Target row. Tests refer to vcsim by
#: this stable name rather than synthesising one per test.
VCSIM_TARGET_NAME: str = "vcsim-acceptance"

#: Probe fingerprint persisted on the seeded :class:`Target`.
#:
#: The G0.6 resolver
#: (:func:`~meho_backplane.connectors.resolver.resolve_connector`) binds
#: a target to a connector implementation by matching the target's
#: ``product`` + fingerprinted ``version`` against each connector's
#: advertised ``supported_version_range``. ``VmwareRestConnector``
#: advertises ``">=8.5,<10.0"``, so a target with **no** fingerprint
#: resolves to *zero* candidates → ``NoMatchingConnector`` →
#: ``no_connector``. A real operator-registered vSphere target always
#: carries a probe fingerprint (CLAUDE.md postulate 3 — "targets are
#: matched to connectors via fingerprint"); seeding one here makes the
#: fixture represent a *probed* target rather than a half-registered
#: one. Stored as ``FingerprintResult.model_dump(mode="json")`` — the
#: exact dict shape the probe route persists to ``Target.fingerprint``.
CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="VMware, Inc.",
    product="vcenter",
    version=CANARY_VERSION,
    build="24021000",
    reachable=True,
    probed_at=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    probe_method="rest-probe",
).model_dump(mode="json")

#: Base URL the seeded :class:`Target` points at. Port 443 keeps
#: ``HttpConnector._base_url`` from appending a ``:port`` suffix, so
#: the respx router's ``base_url`` matches the connector's client URL
#: exactly. ``.test.invalid`` (RFC 6761 reserved) guarantees no real
#: network egress even if respx interception ever regressed.
CANARY_BASE_URL: str = "https://vcenter-canary.test.invalid"

#: vCenter REST's modern ``GET /api/vcenter/vm`` returns a bare JSON
#: array. 50 rows matches what the historical vcsim seed topology
#: would have produced, so the agent-flow + JSONFlux assertions
#: (``len(vms) == 50`` / ``handle.total_rows == 50``) are unchanged by
#: the move off vcsim.
CANARY_VMS: list[dict[str, object]] = [
    {
        "vm": f"vm-{i}",
        "name": f"canary-vm-{i:02d}",
        "power_state": "POWERED_ON" if i % 3 else "POWERED_OFF",
        "cpu_count": 2,
        "memory_size_MiB": 4096,
    }
    for i in range(50)
]


#: Descriptor rows the dispatch smoke + JSONFlux force-mode tests
#: probe. Tuple of (op_id, method, path, summary) so the
#: :func:`_insert_dispatch_descriptors` helper can write them in one
#: pass. Every op_id is an ingested-source-kind read; ``handler_ref``
#: is intentionally NULL — ingested ops route through the connector's
#: parent-class :meth:`HttpConnector` machinery, not a dotted handler.
DISPATCH_DESCRIPTORS: tuple[tuple[str, str, str, str], ...] = (
    (
        "GET:/api/about",
        "GET",
        "/api/about",
        "vCenter appliance build / version metadata.",
    ),
    (
        "GET:/vcenter/cluster",
        "GET",
        "/vcenter/cluster",
        "List vCenter clusters with summary properties.",
    ),
    (
        "GET:/vcenter/host",
        "GET",
        "/vcenter/host",
        "List ESXi hosts registered with vCenter.",
    ),
    (
        "GET:/vcenter/datastore",
        "GET",
        "/vcenter/datastore",
        "List datastores known to vCenter.",
    ),
    (
        "GET:/vcenter/network",
        "GET",
        "/vcenter/network",
        "List networks (portgroups, dvportgroups) visible to vCenter.",
    ),
    (
        "GET:/vcenter/vm",
        "GET",
        "/vcenter/vm",
        "List virtual machines registered with vCenter.",
    ),
)


@dataclass(frozen=True)
class IngestedCanaryVcsim:
    """Bundle returned by :func:`ingested_canary_vcsim`.

    Tests consume the fields by name (``operator`` / ``connector_id``
    / ``target_name`` / ``endpoint``) rather than re-deriving them so
    the fixture's contract is one bounded structure rather than four
    overlapping per-attribute fixtures.
    """

    operator: Operator
    connector_id: str
    target_name: str
    base_url: str


async def _insert_dispatch_descriptors(
    descriptors: tuple[tuple[str, str, str, str], ...],
) -> None:
    """Insert built-in :class:`EndpointDescriptor` rows for every entry.

    One :class:`OperationGroup` (``group_key='dispatch-smoke'``,
    ``review_status='enabled'``) bundles every descriptor so the
    dispatcher's group-scoped queries see a consistent shape.

    ``tenant_id`` is NULL for both the group and the descriptors so
    the rows are built-in / globally visible -- matches the canary's
    convention for vSphere connector content.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group = OperationGroup(
            tenant_id=None,
            product=CANARY_PRODUCT,
            version=CANARY_VERSION,
            impl_id=CANARY_IMPL_ID,
            group_key="dispatch-smoke",
            name="Dispatch smoke ops",
            when_to_use=(
                "Read-only ops used by the G3.1-T8 dispatch smoke + "
                "JSONFlux force-mode acceptance tests. Not a production "
                "operator-facing group; seeded by the acceptance suite "
                "via tests.acceptance._canary_fixtures."
            ),
            review_status="enabled",
        )
        session.add(group)
        await session.flush()  # group.id needed for descriptor.group_id

        for op_id, method, path, summary in descriptors:
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=CANARY_PRODUCT,
                version=CANARY_VERSION,
                impl_id=CANARY_IMPL_ID,
                op_id=op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group.id,
                summary=summary,
                description=summary,
                parameter_schema={"type": "object", "properties": {}},
                response_schema={"type": "object"},
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
            )
            session.add(descriptor)

        await session.commit()


@pytest.fixture
async def prewarmed_embeddings(pg_engine: None) -> None:
    """Load the fastembed model before any respx router is active.

    Depends on ``pg_engine`` (not for the DB, but because that fixture
    chain populates the integration settings env —
    ``KEYCLOAK_ISSUER_URL`` et al. — that
    :func:`get_embedding_service` reads via ``get_settings()``).
    Tests listing this before ``ingested_canary_vcsim`` therefore get:
    env ready → model loaded (real network, no respx) → router opens.

    ``search_operations`` (agent-flow step 3) embeds the query via the
    cached :func:`~meho_backplane.retrieval.embedding.get_embedding_service`
    singleton. fastembed fetches the ~120 MB ONNX model from
    huggingface.co on first use; the conftest points the cache at a
    per-test tmpdir, so that fetch is real network. If it happens
    while :func:`ingested_canary_vcsim`'s respx router is active,
    respx's transport patching corrupts the multi-request model
    download (``Could not load model from any source``). Loading the
    model *here* — before the router's context manager opens — means
    the in-process singleton already holds it, so the search under
    respx does zero network.

    Only the agent-flow test depends on this. The dispatch-only smoke
    / JSONFlux tests call ``call_operation`` with a known op_id (no
    embedding path) and deliberately skip the cost. Tests that want it
    must list this fixture **before** ``ingested_canary_vcsim`` in
    their signature so it's set up before the router opens.
    """
    from meho_backplane.retrieval.embedding import get_embedding_service

    await get_embedding_service().encode_one("vmware rest connector warm-up")


@pytest.fixture
def acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the dispatch tests act as.

    ``tenant_admin`` role so the dispatcher's tenant-scoped queries
    succeed; same shape as the canary's ``canary_operator``.
    """
    return Operator(
        sub="g31-t8-acceptance",
        name="G3.1-T8 Acceptance",
        email=None,
        raw_jwt="<acceptance-raw-jwt>",
        tenant_id=CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


async def _vcenter_rest_session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Stub session loader — bypasses the not-yet-wired Vault read.

    The respx router accepts any HTTP basic pair (it never validates
    the credentials), so the values are illustrative only. The
    ``_operator`` parameter matches the threaded ``VsphereSessionLoader``
    signature (G3.9-T1); the stub ignores it.
    """
    return {"username": "canary-svc", "password": "canary-pw"}


def _register_vcenter_rest_routes(mock: respx.MockRouter) -> None:
    """Register the modern (``/api``) vCenter REST surface on *mock*.

    The router answers the connector's session establishment
    (``POST /api/session`` → token; the modern path 200s so the G0.6
    resolver-side mount resolves to ``/api``), the ``aclose()`` revoke
    (``DELETE /api/session``), and the read ops the three dispatch
    modules probe. ``assert_all_called=False`` on the router (set by
    the fixture) means a test that exercises only one op doesn't trip
    on the unused routes.
    """
    mock.post("/api/session").respond(200, json="canary-session-token")
    mock.delete("/api/session").respond(204)
    mock.get("/api/about").respond(
        200,
        json={
            "product": "VMware vCenter Server",
            "version": CANARY_VERSION,
            "build": "24021000",
            "product_line_id": "vpx",
            "api_type": "vcenter",
        },
    )
    mock.get("/api/vcenter/vm").respond(200, json=CANARY_VMS)
    mock.get("/api/vcenter/cluster").respond(
        200, json=[{"cluster": "domain-c1", "name": "canary-cluster"}]
    )
    mock.get("/api/vcenter/host").respond(200, json=[{"host": "host-1", "name": "canary-esx-01"}])
    mock.get("/api/vcenter/datastore").respond(
        200, json=[{"datastore": "datastore-1", "name": "canary-ds-01"}]
    )
    mock.get("/api/vcenter/network").respond(
        200, json=[{"network": "network-1", "name": "VM Network"}]
    )


@pytest.fixture
async def ingested_canary_vcsim(
    pg_engine: None,
    acceptance_operator: Operator,
) -> AsyncIterator[IngestedCanaryVcsim]:
    """Yield a dispatcher-ready vmware-rest setup over a respx-mocked vCenter.

    The fixture name is retained for call-site stability across the
    three dispatch modules; the transport is **respx**, not vcsim.
    govmomi's vcsim does not implement the vCenter REST *resource*
    API (``/api/vcenter/vm`` and friends 404 — it only stubs the
    vAPI session/tagging/content-library subset + the SOAP surface),
    so a vcsim-backed dispatch test of ``GET:/vcenter/vm`` is not
    satisfiable. respx mocks the exact modern REST surface the
    connector calls, so the full agent-flow chain (resolve → group →
    search → ``call_operation`` dispatch) is still exercised
    end-to-end against a realistic wire contract.

    Setup steps:

    1. Insert built-in :class:`EndpointDescriptor` rows for every op
       in :data:`DISPATCH_DESCRIPTORS` (under one enabled
       :class:`OperationGroup`).
    2. Seed a :class:`Target` (with a probe :data:`CANARY_FINGERPRINT`
       so the resolver binds the versioned connector) pointing at
       :data:`CANARY_BASE_URL`.
    3. Resolve + cache the :class:`VmwareRestConnector` instance the
       dispatcher will use and patch only its ``_session_loader``
       (no Vault in the acceptance suite). The httpx client is *not*
       patched — respx intercepts it transparently.
    4. Activate a respx router for :data:`CANARY_BASE_URL` and
       register the modern vCenter REST surface.

    Teardown (inside the active respx router so the ``DELETE``
    session-revoke is intercepted):

    1. ``aclose()`` the connector instance.
    2. ``reset_dispatcher_caches()`` so the next test sees a fresh
       instance.

    The :class:`VmwareRestConnector` registration survives across
    tests under normal operation; the fixture re-imports the
    ``vmware_rest`` package on demand if a sibling test wiped the v2
    registry (the G0.7 canary's autouse cleanup does this).
    """
    await _insert_dispatch_descriptors(DISPATCH_DESCRIPTORS)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=CANARY_OPERATOR_TENANT,
            name=VCSIM_TARGET_NAME,
            aliases=[],
            product=CANARY_PRODUCT,
            host=CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="kv/data/vsphere/vcenter-canary",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._canary_fixtures.ingested_canary_vcsim",
        )
        session.add(target)
        await session.commit()

    # Resolve the connector class from the v2 registry. The
    # vmware_rest package registers itself at module import time;
    # the registration survives across tests unless something
    # explicitly clears it.
    registry = all_connectors_v2()
    connector_cls = registry.get((CANARY_PRODUCT, CANARY_VERSION, CANARY_IMPL_ID))
    if connector_cls is None:
        # Re-import to trigger registration. Safe even if a previous
        # test ran clear_registry() (the canary does this).
        import importlib

        import meho_backplane.connectors.vmware_rest as _vmware_rest_pkg

        importlib.reload(_vmware_rest_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get((CANARY_PRODUCT, CANARY_VERSION, CANARY_IMPL_ID))

    assert connector_cls is VmwareRestConnector, (
        f"expected VmwareRestConnector registered for "
        f"({CANARY_PRODUCT}, {CANARY_VERSION}, {CANARY_IMPL_ID}); "
        f"got {connector_cls!r}"
    )

    # Materialise the cached instance the dispatcher will use; replace
    # only the Vault-backed session loader. respx intercepts the
    # connector's own httpx client, so ``_http_client`` is left intact
    # (which keeps the production follow-redirects + pooling code on
    # the exercised path).
    instance = get_or_create_connector_instance(connector_cls)
    instance._session_loader = _vcenter_rest_session_loader

    # ``assert_all_mocked=False``: requests that don't match a
    # registered route (notably fastembed's one-time model fetch from
    # huggingface.co, which ``search_operations`` triggers in the
    # agent-flow test) pass through to the real transport rather than
    # raising — matching how the rest of the embedding-using
    # acceptance suite already behaves (the conftest points the
    # fastembed cache at a per-test tmpdir; the download is real).
    # ``assert_all_called=False``: a test that exercises one op
    # doesn't trip on the other registered routes.
    async with respx.mock(
        base_url=CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vcenter_rest_routes(mock)
        try:
            yield IngestedCanaryVcsim(
                operator=acceptance_operator,
                connector_id=CANARY_CONNECTOR_ID,
                target_name=VCSIM_TARGET_NAME,
                base_url=CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()
