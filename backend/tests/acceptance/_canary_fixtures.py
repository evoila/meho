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
Tests that need search-quality (the agent-flow E2E) drive their
own copy of the canary's ingest pattern -- see
:mod:`tests.acceptance.test_vmware_rest_agent_flow_e2e` for that
path.

The agent-flow E2E test does NOT consume this module's fixture; it
uses the canary's ingest pattern (copied compactly) so the
:func:`~meho_backplane.operations.meta_tools.search_operations` call
has BM25 + cosine signal to rank against. Both paths converge on the
same patched :class:`VmwareRestConnector` instance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from tests.acceptance._vcsim import VcsimEndpoint, patch_vmware_connector_for_vcsim

__all__ = [
    "CANARY_CONNECTOR_ID",
    "CANARY_IMPL_ID",
    "CANARY_OPERATOR_TENANT",
    "CANARY_PRODUCT",
    "CANARY_VERSION",
    "DISPATCH_DESCRIPTORS",
    "VCSIM_TARGET_NAME",
    "IngestedCanaryVcsim",
    "ingested_canary_vcsim",
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
    endpoint: VcsimEndpoint


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


@pytest.fixture
async def ingested_canary_vcsim(
    pg_engine: None,
    acceptance_operator: Operator,
    vcsim_endpoint: VcsimEndpoint,
) -> AsyncIterator[IngestedCanaryVcsim]:
    """Yield a dispatcher-ready vcsim setup.

    Setup steps:

    1. Insert built-in :class:`EndpointDescriptor` rows for every
       op in :data:`DISPATCH_DESCRIPTORS` (under one enabled
       :class:`OperationGroup`).
    2. Seed a :class:`Target` row pointing at the live vcsim
       endpoint (host/port/auth_model).
    3. Resolve + cache the :class:`VmwareRestConnector` instance the
       dispatcher will use; patch its ``_session_loader`` to return
       vcsim's no-auth credentials and its ``_http_client`` to skip
       TLS verification of vcsim's self-signed cert.

    Teardown:

    1. ``aclose()`` the patched connector instance (releases the
       httpx pool, revokes cached sessions).
    2. ``reset_dispatcher_caches()`` so the next test sees a fresh,
       unpatched instance.

    The :class:`VmwareRestConnector` registration survives across
    tests under normal operation (no test runs ``clear_registry()``
    against the v2 registry); the fixture re-imports the
    ``vmware_rest`` package on demand if a sibling test wiped the
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
            host=vcsim_endpoint.host,
            port=vcsim_endpoint.port,
            fqdn=None,
            secret_ref="kv/data/vsphere/vcsim-acceptance",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
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

    # Materialise the cached instance the dispatcher will use, then
    # patch it. ``get_or_create_connector_instance`` lazily
    # constructs ``VmwareRestConnector()`` on first call; the default
    # ``_session_loader`` is the not-yet-implemented Vault reader,
    # which the patch immediately replaces.
    instance = get_or_create_connector_instance(connector_cls)
    patch_vmware_connector_for_vcsim(instance, vcsim_endpoint)

    try:
        yield IngestedCanaryVcsim(
            operator=acceptance_operator,
            connector_id=CANARY_CONNECTOR_ID,
            target_name=VCSIM_TARGET_NAME,
            endpoint=vcsim_endpoint,
        )
    finally:
        await instance.aclose()
        reset_dispatcher_caches()
