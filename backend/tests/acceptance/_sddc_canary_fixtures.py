# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.5 SDDC Manager dispatch tests.

Two SDDC Manager acceptance modules (dispatch smoke + JSONFlux force-handle)
share the same plumbing: a registered
:class:`~meho_backplane.connectors.sddc_manager.SddcManagerConnector` instance
with a stub credentials loader (so no Vault read is required), a probed
:class:`~meho_backplane.db.models.Target` row, the 4 curated
:class:`~meho_backplane.db.models.EndpointDescriptor` rows from
:data:`~meho_backplane.connectors.sddc_manager.core_ops.SDDC_CORE_OPS`, and a
:mod:`respx`-mocked SDDC Manager REST surface answering each of the 4 curated
read ops.

SDDC Manager is token-only: the connector establishes a session at
``POST /v1/tokens`` (the ``session_login_token`` scheme, #2290) and sends the
minted ``accessToken`` as ``Authorization: Bearer`` on every subsequent
request. The stub credentials loader bypasses the Vault-backed loader; the
respx router registers the token-mint route plus each op path (the Bearer
header is not asserted by default).

Why a minimal direct-insert path (not full G0.7 canary ingest)
==============================================================

The full VCF API spec ingest needs the SDDC Manager 9.0 OpenAPI spec file
reachable on the CI runner. Until the spec-shelf is wired to the
meho-runners pool, the dispatch leg is exercised against a minimal
direct-insert path that seeds the 4 curated endpoint_descriptor rows (plus
the hosts breadth row the JSONFlux test dispatches) by
hand. Same pattern :mod:`tests.acceptance._nsx_canary_fixtures` established
for NSX (which also has no public CI simulator).

``EndpointDescriptor.product`` note
====================================

Rows are inserted with ``product=SDDC_PRODUCT="sddc"`` — the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` derives from
``"sddc-rest-9.0"`` (first hyphen-segment of impl_id). The :class:`Target`
row uses ``product="sddc"`` so the resolver finds
:class:`SddcManagerConnector` (registered with ``product="sddc"``
in the v2 registry). These product values serve different purposes; both
are required for end-to-end dispatch to succeed.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.sddc_manager import (
    SDDC_CONNECTOR_ID,
    SDDC_CORE_GROUPS,
    SDDC_CORE_OPS,
    SDDC_IMPL_ID,
    SDDC_PRODUCT,
    SDDC_VERSION,
    SddcManagerConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

__all__ = [
    "SDDC_CANARY_BASE_URL",
    "SDDC_CANARY_BUNDLES",
    "SDDC_CANARY_DOMAINS",
    "SDDC_CANARY_FINGERPRINT",
    "SDDC_CANARY_HOSTS",
    "SDDC_CANARY_OPERATOR_TENANT",
    "SDDC_FORCE_HANDLE_LIST_OP_ID",
    "SDDC_TARGET_NAME",
    "IngestedSddcCanary",
    "ingested_sddc_canary",
    "sddc_acceptance_operator",
]

#: Tenant the SDDC Manager dispatch tests act under.
SDDC_CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000fe")

#: Stable :class:`Target.name` for the seeded SDDC Manager target.
SDDC_TARGET_NAME: str = "sddc-acceptance"

#: ``.test.invalid`` (RFC 6761 reserved) so no real network egress fires.
SDDC_CANARY_BASE_URL: str = "https://sddc-canary.test.invalid"

#: Persisted as ``Target.fingerprint`` so the resolver binds
#: :class:`SddcManagerConnector` (``supported_version_range=">=9.0,<10.0"``).
SDDC_CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="vmware",
    product="sddc",
    version=SDDC_VERSION,
    build=None,
    reachable=True,
    probed_at=datetime(2026, 5, 19, 10, 0, 0, tzinfo=UTC),
    probe_method="GET /v1/sddc-managers",
    extras={
        "id": "sddc-canary-id",
        "fqdn": "sddc-canary.test.invalid",
        "management_domain": "MGMT",
        "management_domain_id": "domain-mgmt",
    },
).model_dump(mode="json")

#: The list op the JSONFlux force-handle test dispatches. Hosts is the
#: largest list surface in a typical VCF deployment (dozens or hundreds of
#: rows), mirroring the NSX segment-list choice.
SDDC_FORCE_HANDLE_LIST_OP_ID: str = "GET:/v1/hosts"

#: Synthetic release info.
SDDC_CANARY_RELEASE: dict[str, object] = {
    "version": "9.0.0.0-24000000",
    "releaseDate": "2026-01-15",
    "description": "VMware Cloud Foundation 9.0",
    "bom": [
        {"componentType": "VCENTER", "componentVersion": "8.0.3"},
        {"componentType": "NSX", "componentVersion": "4.2.1"},
        {"componentType": "ESXI", "componentVersion": "8.0.3"},
    ],
}

#: Synthetic SDDC Manager appliance list.
SDDC_CANARY_MANAGERS: dict[str, object] = {
    "elements": [
        {
            "id": "sddc-canary-id",
            "fqdn": "sddc-canary.test.invalid",
            "ipAddress": "192.168.1.5",
            "version": "9.0.0.0-24000000",
            "domain": {"id": "domain-mgmt", "name": "MGMT"},
        }
    ],
    "pageMetadata": {"pageNumber": 0, "pageSize": 10, "totalElements": 1, "totalPages": 1},
}

#: Synthetic domain list — one management domain + one workload domain.
SDDC_CANARY_DOMAINS: dict[str, object] = {
    "elements": [
        {
            "id": "domain-mgmt",
            "name": "MGMT",
            "type": "MANAGEMENT",
            "vcenters": [{"id": "vcenter-mgmt", "fqdn": "vcenter-mgmt.test.invalid"}],
            "nsxtCluster": {"id": "nsx-mgmt", "vipFqdn": "nsx-mgmt.test.invalid"},
        },
        {
            "id": "domain-wld01",
            "name": "WLD-01",
            "type": "WORKLOAD",
            "vcenters": [{"id": "vcenter-wld01", "fqdn": "vcenter-wld01.test.invalid"}],
            "nsxtCluster": {"id": "nsx-wld01", "vipFqdn": "nsx-wld01.test.invalid"},
        },
    ],
    "pageMetadata": {"pageNumber": 0, "pageSize": 10, "totalElements": 2, "totalPages": 1},
}

#: Synthetic domain detail for ``domain-mgmt``.
SDDC_CANARY_DOMAIN_DETAIL: dict[str, object] = {
    "id": "domain-mgmt",
    "name": "MGMT",
    "type": "MANAGEMENT",
    "vcenters": [{"id": "vcenter-mgmt", "fqdn": "vcenter-mgmt.test.invalid"}],
    "nsxtCluster": {"id": "nsx-mgmt", "vipFqdn": "nsx-mgmt.test.invalid"},
    "clusters": [{"id": "cluster-mgmt-01", "name": "Cluster-MGMT-01"}],
    "ssoId": "vsphere.local",
    "ssoName": "vsphere.local",
}

#: Synthetic cluster list.
SDDC_CANARY_CLUSTERS: dict[str, object] = {
    "elements": [
        {
            "id": "cluster-mgmt-01",
            "name": "Cluster-MGMT-01",
            "primaryDatastoreType": "VMFS_FC",
            "domainId": "domain-mgmt",
            "hosts": [{"id": f"host-{i}"} for i in range(4)],
        },
        {
            "id": "cluster-wld-01",
            "name": "Cluster-WLD-01",
            "primaryDatastoreType": "VSAN",
            "domainId": "domain-wld01",
            "hosts": [{"id": f"host-{i}"} for i in range(4, 8)],
        },
    ],
    "pageMetadata": {"pageNumber": 0, "pageSize": 10, "totalElements": 2, "totalPages": 1},
}

#: Synthetic host list — 12 hosts so the force-handle reducer sees a
#: populated set with a sample-row slice.
SDDC_CANARY_HOSTS: dict[str, object] = {
    "elements": [
        {
            "id": f"host-{i}",
            "fqdn": f"esx-canary-{i:02d}.test.invalid",
            "esxiVersion": "8.0.3",
            "ipAddresses": [{"ipAddress": f"192.168.10.{10 + i}", "type": "MANAGEMENT"}],
            "domain": {"id": "domain-mgmt" if i < 4 else "domain-wld01"},
            "cluster": {"id": "cluster-mgmt-01" if i < 4 else "cluster-wld-01"},
            "networkPool": {"id": "pool-01"},
            "status": "ASSIGNED",
        }
        for i in range(12)
    ],
    "pageMetadata": {"pageNumber": 0, "pageSize": 20, "totalElements": 12, "totalPages": 1},
}

#: Synthetic network pool list.
SDDC_CANARY_NETWORK_POOLS: dict[str, object] = {
    "elements": [
        {
            "id": "pool-01",
            "name": "NetworkPool-01",
            "networks": [
                {
                    "type": "VSAN",
                    "vlanId": 1011,
                    "subnet": "192.168.20.0",
                    "mask": "255.255.255.0",
                    "gateway": "192.168.20.1",
                    "ipPools": [{"start": "192.168.20.10", "end": "192.168.20.50"}],
                }
            ],
        }
    ],
    "pageMetadata": {"pageNumber": 0, "pageSize": 10, "totalElements": 1, "totalPages": 1},
}

#: Synthetic LCM bundle list.
SDDC_CANARY_BUNDLES: dict[str, object] = {
    "elements": [
        {
            "id": "bundle-vcf-9-0-1",
            "type": "VMWARE_SOFTWARE",
            "version": "9.0.1.0",
            "description": "VCF 9.0.1 cumulative update",
            "sizeMB": 14500,
            "downloadStatus": "SUCCESSFUL",
            "isCumulative": True,
            "isCompliant": False,
            "applicabilityStatus": "APPLICABLE",
            "components": [
                {"componentType": "VCENTER", "componentVersion": "8.0.3.1"},
                {"componentType": "NSX", "componentVersion": "4.2.2"},
            ],
        }
    ],
    "pageMetadata": {"pageNumber": 0, "pageSize": 10, "totalElements": 1, "totalPages": 1},
}

#: Synthetic VCF task list.
SDDC_CANARY_TASKS: dict[str, object] = {
    "elements": [
        {
            "id": "task-expand-wld01",
            "name": "Expand Workload Domain WLD-01",
            "status": "Successful",
            "type": "WORKLOAD_DOMAIN_EXPAND",
            "creationTimestamp": "2026-05-19T08:00:00.000Z",
            "completionTimestamp": "2026-05-19T08:45:00.000Z",
            "subtasks": [],
            "errors": [],
        }
    ],
    "pageMetadata": {"pageNumber": 0, "pageSize": 10, "totalElements": 1, "totalPages": 1},
}


@dataclass(frozen=True)
class IngestedSddcCanary:
    """Bundle returned by :func:`ingested_sddc_canary`."""

    operator: Operator
    connector_id: str
    target_name: str
    base_url: str


async def _insert_sddc_descriptors() -> None:
    """Seed the 4 curated SDDC Manager core ops + their groups as enabled rows.

    One :class:`OperationGroup` per entry in :data:`SDDC_CORE_GROUPS`
    (``review_status='enabled'``), one :class:`EndpointDescriptor` per entry
    in :data:`SDDC_CORE_OPS` (``is_enabled=True``, ``source_kind='ingested'``,
    ``handler_ref=None``).

    Rows use ``product=SDDC_PRODUCT="sddc"`` (from
    :func:`parse_connector_id("sddc-rest-9.0")`), not the connector class's
    ``product="sddc"``. The target row uses ``product="sddc"``
    so the resolver finds :class:`SddcManagerConnector`.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group in SDDC_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=SDDC_PRODUCT,
                version=SDDC_VERSION,
                impl_id=SDDC_IMPL_ID,
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in SDDC_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=SDDC_PRODUCT,
                version=SDDC_VERSION,
                impl_id=SDDC_IMPL_ID,
                op_id=op.op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[op.group_key],
                summary=f"SDDC Manager core op {op.op_id} (curated read).",
                description=f"SDDC Manager core op {op.op_id} (curated read).",
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=op.llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=["spec:sddc-manager-9.0/api.yaml"],
            )
            session.add(descriptor)

        # The JSONFlux force-handle test dispatches the ingested
        # ``GET:/v1/hosts`` breadth op. Since #2306 promoted hosts to a
        # first-class typed op (``sddc.host.list``), the ingested row is no
        # longer part of the curated SDDC_CORE_OPS set -- but it still exists
        # as browse breadth. Seed it explicitly (ungrouped) so the JSONFlux
        # test exercises the ingested-breadth surface, proving the two
        # surfaces coexist without shadowing.
        if SDDC_FORCE_HANDLE_LIST_OP_ID not in {op.op_id for op in SDDC_CORE_OPS}:
            force_method, force_path = SDDC_FORCE_HANDLE_LIST_OP_ID.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=None,
                    product=SDDC_PRODUCT,
                    version=SDDC_VERSION,
                    impl_id=SDDC_IMPL_ID,
                    op_id=SDDC_FORCE_HANDLE_LIST_OP_ID,
                    source_kind="ingested",
                    method=force_method,
                    path=force_path,
                    handler_ref=None,
                    group_id=None,
                    summary=f"SDDC Manager ingested breadth op {SDDC_FORCE_HANDLE_LIST_OP_ID}.",
                    description=f"SDDC Manager ingested breadth op {SDDC_FORCE_HANDLE_LIST_OP_ID}.",
                    parameter_schema=_param_schema_for(force_path),
                    response_schema={"type": "object"},
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=True,
                    tags=["spec:sddc-manager-9.0/api.yaml"],
                )
            )
        await session.commit()


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` for each ``{var}`` in *path*.

    Mirrors :func:`tests.acceptance._nsx_canary_fixtures._param_schema_for`.
    Only ``GET:/v1/domains/{id}`` has a path parameter in the curated SDDC
    Manager core; all other ops return the empty-properties schema.
    """
    placeholders = _PATH_VAR_RE.findall(path)
    if not placeholders:
        return {"type": "object", "properties": {}}
    return {
        "type": "object",
        "properties": {
            name: {"type": "string", "x-meho-param-loc": "path"} for name in placeholders
        },
        "required": list(placeholders),
    }


async def _sddc_credentials_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Stub credentials loader — bypasses the live operator-context Vault read.

    The 2-arg signature matches the
    :class:`~meho_backplane.connectors.sddc_manager.session.SddcCredentialsLoader`
    G3.10-T1 (#945) introduced.
    """
    return {"username": "sddc-canary-svc", "password": "sddc-canary-pw"}


#: The synthetic ``accessToken`` the mocked ``POST /v1/tokens`` mints. Opaque
#: to the dispatch leg — the tests assert only that requests carry a Bearer
#: header, not the token value.
SDDC_CANARY_ACCESS_TOKEN: str = "sddc-canary-access-token"


def _register_sddc_routes(mock: respx.MockRouter) -> None:
    """Register the session-mint route + the SDDC Manager read-op routes on *mock*.

    SDDC Manager is token-only: the connector first POSTs ``/v1/tokens`` to
    mint an ``accessToken`` (the ``session_login_token`` scheme, #2290), then
    sends it as ``Authorization: Bearer`` on each op request. Each route returns
    a pre-seeded JSON body matching the rough shape SDDC Manager returns for
    that path family.

    The ``GET:/v1/domains/{id}`` route is registered for the specific
    ``domain-mgmt`` id the dispatch smoke test uses as its path parameter.
    """
    mock.post("/v1/tokens").respond(200, json={"accessToken": SDDC_CANARY_ACCESS_TOKEN})
    mock.get("/v1/releases/system").respond(200, json=SDDC_CANARY_RELEASE)
    mock.get("/v1/sddc-managers").respond(200, json=SDDC_CANARY_MANAGERS)
    mock.get("/v1/domains").respond(200, json=SDDC_CANARY_DOMAINS)
    # Path-templated domain detail — match the specific id the smoke test
    # passes as the ``{id}`` parameter.
    mock.get("/v1/domains/domain-mgmt").respond(200, json=SDDC_CANARY_DOMAIN_DETAIL)
    mock.get("/v1/clusters").respond(200, json=SDDC_CANARY_CLUSTERS)
    mock.get("/v1/hosts").respond(200, json=SDDC_CANARY_HOSTS)
    mock.get("/v1/network-pools").respond(200, json=SDDC_CANARY_NETWORK_POOLS)
    mock.get("/v1/bundles").respond(200, json=SDDC_CANARY_BUNDLES)
    mock.get("/v1/tasks").respond(200, json=SDDC_CANARY_TASKS)


@pytest.fixture
def sddc_acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the SDDC Manager dispatch tests act as."""
    return Operator(
        sub="g35-sddc-acceptance",
        name="G3.5-T5 SDDC Acceptance",
        email=None,
        raw_jwt="<sddc-acceptance-raw-jwt>",
        tenant_id=SDDC_CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture
async def ingested_sddc_canary(
    pg_engine: None,
    sddc_acceptance_operator: Operator,
) -> AsyncIterator[IngestedSddcCanary]:
    """Yield a dispatcher-ready SDDC Manager setup over a respx-mocked appliance.

    Setup mirrors :func:`tests.acceptance._nsx_canary_fixtures.ingested_nsx_canary`:

    1. Insert built-in :class:`OperationGroup` + :class:`EndpointDescriptor`
       rows for the 4 curated SDDC Manager core ops (plus the hosts breadth row).
    2. Seed a :class:`Target` with ``product="sddc"`` and the
       :data:`SDDC_CANARY_FINGERPRINT` so the resolver binds
       :class:`SddcManagerConnector`.
    3. Resolve + cache the :class:`SddcManagerConnector` instance the
       dispatcher will use, patching only its ``_credentials_loader``.
    4. Activate a respx router for :data:`SDDC_CANARY_BASE_URL` and
       register the SDDC Manager REST surface.
    """
    await _insert_sddc_descriptors()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=SDDC_CANARY_OPERATOR_TENANT,
            name=SDDC_TARGET_NAME,
            aliases=[],
            product="sddc",
            host=SDDC_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="sddc-manager/sddc-canary",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=SDDC_CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._sddc_canary_fixtures.ingested_sddc_canary",
        )
        session.add(target)
        await session.commit()

    registry = all_connectors_v2()
    connector_cls = registry.get(("sddc", SDDC_VERSION, "sddc-rest"))
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.sddc_manager as _sddc_pkg

        importlib.reload(_sddc_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get(("sddc", SDDC_VERSION, "sddc-rest"))

    assert connector_cls is SddcManagerConnector, (
        f"expected SddcManagerConnector registered for "
        f"(sddc-manager, {SDDC_VERSION}, sddc-rest); got {connector_cls!r}"
    )

    instance = get_or_create_connector_instance(connector_cls)
    instance._credentials_loader = _sddc_credentials_loader  # type: ignore[attr-defined]

    async with respx.mock(
        base_url=SDDC_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_sddc_routes(mock)
        try:
            yield IngestedSddcCanary(
                operator=sddc_acceptance_operator,
                connector_id=SDDC_CONNECTOR_ID,
                target_name=SDDC_TARGET_NAME,
                base_url=SDDC_CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()
