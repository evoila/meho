# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.6 vROps dispatch tests.

Two vROps acceptance modules (dispatch smoke + JSONFlux force-handle)
share the same plumbing: a registered
:class:`~meho_backplane.connectors.vcf_operations.VcfOperationsConnector`
instance with a stub credentials loader (so no Vault read is required),
a probed :class:`~meho_backplane.db.models.Target` row, the 6 ingested
browse-breadth :class:`~meho_backplane.db.models.EndpointDescriptor` rows
from :data:`_VROPS_SEED_OPS`, and a :mod:`respx`-mocked vROps REST surface
answering each of the 6 read ops.

vROps uses HTTP Basic on every request — no session establish call is
needed. The stub credentials loader bypasses the Vault-backed loader;
the respx router matches requests by path.

Why a minimal direct-insert path (not full G0.7 canary ingest)
==============================================================

The full vROps suite-api spec ingest via :class:`IngestionPipelineService`
needs the vROps OpenAPI spec reachable on the CI runner plus a live
LLM for the grouping pass. Until the spec-shelf is wired to the
meho-runners pool, the dispatch leg is exercised against a minimal
direct-insert path that seeds the 8 curated endpoint_descriptor rows
by hand. Same pattern :mod:`tests.acceptance._nsx_canary_fixtures` and
:mod:`tests.acceptance._harbor_canary_fixtures` established.

``EndpointDescriptor.product`` vs ``Target.product`` note
==========================================================

Rows are inserted with ``product=VROPS_PRODUCT="vrops"`` — the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` derives
from ``"vrops-rest-9.0"`` (first hyphen-segment of impl_id
``"vrops-rest"``). Since #1814 (Initiative #1810) this equals
:attr:`VcfOperationsConnector.product` (``"vrops"``) — the connector
registers under the short, dispatch-canonical token, so the registry
key and the parser-derived spelling agree.

The :class:`Target` row uses ``product=_TARGET_PRODUCT="vrops"``
so the resolver finds :class:`VcfOperationsConnector` (registered
with ``product="vrops"`` in the v2 registry). The descriptor /
group rows use ``product=VROPS_PRODUCT="vrops"`` so the dispatcher's
``parse_connector_id``-derived lookup hits them.
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
from meho_backplane.connectors.vcf_operations import (
    VROPS_CONNECTOR_ID,
    VROPS_IMPL_ID,
    VROPS_PRODUCT,
    VROPS_VERSION,
    VcfOperationsConnector,
    VcfOperationsTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

__all__ = [
    "VROPS_CANARY_ALERTDEFS",
    "VROPS_CANARY_ALERTS",
    "VROPS_CANARY_BASE_URL",
    "VROPS_CANARY_CORE_OP_IDS",
    "VROPS_CANARY_FINGERPRINT",
    "VROPS_CANARY_OPERATOR_TENANT",
    "VROPS_CANARY_RECOMMENDATIONS",
    "VROPS_CANARY_RESOURCES",
    "VROPS_CANARY_SUPERMETRICS",
    "VROPS_CANARY_SYMPTOMS",
    "VROPS_FORCE_HANDLE_LIST_OP_ID",
    "VROPS_TARGET_NAME",
    "IngestedVropsCanary",
    "ingested_vrops_canary",
    "vrops_acceptance_operator",
]

#: Tenant the vROps dispatch tests act under.
VROPS_CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000fc")

#: The connector-class-advertised ``product`` key (as registered in the
#: v2 registry and as the resolver expects on a :class:`Target`). Distinct
#: from :data:`VROPS_PRODUCT` (``"vrops"``), which is what
#: :func:`parse_connector_id` derives from the connector_id slug — the
#: SDDC Manager precedent codified this product-key split.
_TARGET_PRODUCT: str = "vrops"

#: Stable :class:`Target.name` for the seeded vROps target.
VROPS_TARGET_NAME: str = "vrops-acceptance"

#: ``.test.invalid`` (RFC 6761 reserved) so no real network egress fires.
VROPS_CANARY_BASE_URL: str = "https://vrops-canary.test.invalid"

#: Persisted as :attr:`Target.fingerprint` — what the connector resolver
#: reads to bind the target's ``product`` + ``version`` against the
#: :class:`VcfOperationsConnector.supported_version_range` advertisement
#: (``>=9.0,<10.0``). The probe route normally writes this dict at
#: first-probe time; the dispatch tests seed it directly so the resolver
#: binds the connector without a real probe round-trip.
VROPS_CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="vmware",
    product="vrops",
    version="9.0.0.1.23456789",
    build="23456789",
    reachable=True,
    probed_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
    probe_method="GET /suite-api/api/versions/current",
    extras={"humanly_readable_release_name": "VMware Aria Operations 9.0"},
).model_dump(mode="json")

#: The list op the JSONFlux force-handle test dispatches. Resources is
#: the largest surface in a real vROps deployment (hundreds to
#: thousands of monitored objects), mirroring the NSX segment-list and
#: SDDC host-list choices.
VROPS_FORCE_HANDLE_LIST_OP_ID: str = "GET:/suite-api/api/resources"

#: Synthetic resource list — 10 rows so the force-handle reducer sees
#: a populated set with a sample-row slice. vROps' suite-api wraps
#: resources under a ``resourceList`` key (not ``results`` / ``value``);
#: the force-handle test's reducer falls through the unknown-key branch
#: but still counts the rows correctly via the dict-with-list discovery
#: loop.
VROPS_CANARY_RESOURCES: dict[str, object] = {
    "resourceList": [
        {
            "identifier": f"00000000-0000-4000-8000-{i:012x}",
            "resourceKey": {
                "name": f"vm-canary-{i:02d}",
                "adapterKindKey": "VMWARE",
                "resourceKindKey": "VirtualMachine",
                "resourceIdentifiers": [
                    {"identifierType": {"name": "VMEntityObjectID"}, "value": f"vm-{i}"},
                ],
            },
            "creationTime": 1716480000000 + i,
            "resourceStatusStates": [
                {
                    "adapterInstanceId": f"00000000-0000-4000-8000-{i:012x}",
                    "resourceStatus": "DATA_RECEIVING",
                    "resourceState": "STARTED",
                }
            ],
            "credentialInstanceId": None,
        }
        for i in range(10)
    ],
    "pageInfo": {"totalCount": 10, "page": 0, "pageSize": 1000},
    "links": [{"href": "/suite-api/api/resources", "rel": "SELF", "name": "current"}],
}

#: Synthetic resource detail — one row matching the first resource in
#: the list above.
VROPS_CANARY_RESOURCE_DETAIL: dict[str, object] = {
    "identifier": "00000000-0000-4000-8000-000000000000",
    "resourceKey": {
        "name": "vm-canary-00",
        "adapterKindKey": "VMWARE",
        "resourceKindKey": "VirtualMachine",
        "resourceIdentifiers": [{"identifierType": {"name": "VMEntityObjectID"}, "value": "vm-0"}],
    },
    "creationTime": 1716480000000,
    "resourceStatusStates": [
        {
            "adapterInstanceId": "00000000-0000-4000-8000-000000000000",
            "resourceStatus": "DATA_RECEIVING",
            "resourceState": "STARTED",
        }
    ],
    "credentialInstanceId": None,
}

#: Synthetic alert list — two active alerts.
VROPS_CANARY_ALERTS: dict[str, object] = {
    "alerts": [
        {
            "alertId": "00000000-0000-4000-9000-000000000001",
            "alertDefinitionId": "AlertDefinition-VirtualMachine-CPU-Demand",
            "alertDefinitionName": "Virtual machine has high CPU demand",
            "alertLevel": 3,
            "alertImpact": "performance",
            "resourceId": "00000000-0000-4000-8000-000000000000",
            "startTimeUTC": 1716480000000,
            "cancelTimeUTC": 0,
            "status": "ACTIVE",
            "controlState": "OPEN",
        },
        {
            "alertId": "00000000-0000-4000-9000-000000000002",
            "alertDefinitionId": "AlertDefinition-Datastore-Capacity",
            "alertDefinitionName": "Datastore is running out of capacity",
            "alertLevel": 4,
            "alertImpact": "capacity",
            "resourceId": "00000000-0000-4000-8000-000000000001",
            "startTimeUTC": 1716480100000,
            "cancelTimeUTC": 0,
            "status": "ACTIVE",
            "controlState": "OPEN",
        },
    ],
    "pageInfo": {"totalCount": 2, "page": 0, "pageSize": 1000},
    "links": [{"href": "/suite-api/api/alerts", "rel": "SELF", "name": "current"}],
}

#: Synthetic alert-definition list — one entry covering the two alerts above.
VROPS_CANARY_ALERTDEFS: dict[str, object] = {
    "alertDefinitions": [
        {
            "id": "AlertDefinition-VirtualMachine-CPU-Demand",
            "name": "Virtual machine has high CPU demand",
            "description": "Fires when CPU demand exceeds the configured threshold.",
            "adapterKindKey": "VMWARE",
            "resourceKindKey": "VirtualMachine",
            "waitCycles": 1,
            "cancelCycles": 1,
            "type": 16,
            "subType": 18,
            "states": [
                {
                    "severity": "WARNING",
                    "base-symptom-set": {
                        "type": "SYMPTOM_SET",
                        "relation": "SELF",
                        "aggregation": "ALL",
                        "symptomSetOperator": "AND",
                        "symptomDefinitionIds": ["SymptomDefinition-CPU-Demand"],
                    },
                    "recommendation-priority-map": [
                        {"recommendationId": "Rec-CPU-Demand", "priority": 1}
                    ],
                    "impact": {"impactType": "RISK"},
                }
            ],
        }
    ],
    "pageInfo": {"totalCount": 1, "page": 0, "pageSize": 1000},
    "links": [{"href": "/suite-api/api/alertdefinitions", "rel": "SELF", "name": "current"}],
}

#: Synthetic symptom list — one firing symptom.
VROPS_CANARY_SYMPTOMS: dict[str, object] = {
    "symptoms": [
        {
            "id": "00000000-0000-4000-a000-000000000001",
            "symptomDefinitionId": "SymptomDefinition-CPU-Demand",
            "symptomDefinitionName": "CPU demand high",
            "resourceId": "00000000-0000-4000-8000-000000000000",
            "alertId": "00000000-0000-4000-9000-000000000001",
            "startTimeUTC": 1716480000000,
            "cancelTimeUTC": 0,
            "severity": "WARNING",
            "statusType": "ACTIVE",
            "controlState": "OPEN",
        }
    ],
    "pageInfo": {"totalCount": 1, "page": 0, "pageSize": 1000},
    "links": [{"href": "/suite-api/api/symptoms", "rel": "SELF", "name": "current"}],
}

#: Synthetic recommendation list — one operator-facing remediation hint.
VROPS_CANARY_RECOMMENDATIONS: dict[str, object] = {
    "recommendations": [
        {
            "id": "Rec-CPU-Demand",
            "description": (
                "Reduce the workload on the virtual machine or increase its CPU allocation."
            ),
            "actionId": None,
        }
    ],
    "pageInfo": {"totalCount": 1, "page": 0, "pageSize": 1000},
    "links": [{"href": "/suite-api/api/recommendations", "rel": "SELF", "name": "current"}],
}

#: Synthetic super-metric list — one user-defined formula.
VROPS_CANARY_SUPERMETRICS: dict[str, object] = {
    "superMetrics": [
        {
            "id": "00000000-0000-4000-b000-000000000001",
            "name": "host-cpu-overprovision-ratio",
            "description": "Ratio of provisioned vCPUs to physical cores across hosts.",
            "formula": "sum(${this, metric=cpu|provisioned}) / sum(${this, metric=cpu|physical})",
            "modificationTime": 1716480000000,
        }
    ],
    "pageInfo": {"totalCount": 1, "page": 0, "pageSize": 1000},
    "links": [{"href": "/suite-api/api/supermetrics", "rel": "SELF", "name": "current"}],
}

#: Synthetic version response (the ``vrops.about`` op).
VROPS_CANARY_VERSION: dict[str, object] = {
    "releaseName": "9.0.0.1.23456789",
    "buildNumber": 23456789,
    "humanlyReadableReleaseName": "VMware Aria Operations 9.0",
}


@dataclass(frozen=True)
class IngestedVropsCanary:
    """Bundle returned by :func:`ingested_vrops_canary`.

    Same shape as :class:`tests.acceptance._nsx_canary_fixtures.IngestedNsxCanary`
    so the assertion patterns transfer over verbatim.
    """

    operator: Operator
    connector_id: str
    target_name: str
    base_url: str


#: Ingested browse-breadth seed data for the vROps dispatch canary — the six
#: ``source_kind="ingested"`` read ops (and their five groups) declined from
#: typed conversion on #2303 but kept browsable. Relocated here from the
#: retired ``vcf_operations.core_ops`` curation apparatus (#2358): this is
#: test-only fixture material describing the ``EndpointDescriptor`` rows the
#: dispatch tests seed and mock. ``(group_key, name, when_to_use)``.
_VROPS_SEED_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("vrops-resources", "vROps Resources", "Monitored resource inventory."),
    ("vrops-alert-definitions", "vROps Alert Definitions", "Configured alert definitions."),
    ("vrops-symptoms", "vROps Symptoms", "Symptom definitions."),
    ("vrops-recommendations", "vROps Recommendations", "Remediation recommendations."),
    ("vrops-supermetrics", "vROps Super Metrics", "Super-metric definitions."),
)

#: ``(op_id, group_key)`` for each ingested browse-breadth vROps read op. The
#: ``vrops-resources`` group carries two ops (list + get by id).
_VROPS_SEED_OPS: tuple[tuple[str, str], ...] = (
    ("GET:/suite-api/api/resources", "vrops-resources"),
    ("GET:/suite-api/api/resources/{id}", "vrops-resources"),
    ("GET:/suite-api/api/alertdefinitions", "vrops-alert-definitions"),
    ("GET:/suite-api/api/symptoms", "vrops-symptoms"),
    ("GET:/suite-api/api/recommendations", "vrops-recommendations"),
    ("GET:/suite-api/api/supermetrics", "vrops-supermetrics"),
)

#: Op ids the vROps dispatch/e2e/smoke tests parametrize over (relocated from
#: ``tuple(op.op_id for op in VROPS_CORE_OPS)``).
VROPS_CANARY_CORE_OP_IDS: tuple[str, ...] = tuple(op_id for op_id, _ in _VROPS_SEED_OPS)


async def _insert_vrops_descriptors() -> None:
    """Seed the 6 ingested vROps browse-breadth ops + their groups as enabled rows.

    One :class:`OperationGroup` per entry in :data:`_VROPS_SEED_GROUPS`
    (``review_status='enabled'``), one :class:`EndpointDescriptor` per
    entry in :data:`_VROPS_SEED_OPS`
    (``is_enabled=True``, ``source_kind='ingested'``, ``handler_ref=None``).

    The ``vrops-resources`` group carries two ops (list + get by id);
    the helper coalesces them into one inserted group row so the FK to
    ``operation_group.id`` resolves correctly on both ops.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group_key, name, when_to_use in _VROPS_SEED_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=VROPS_PRODUCT,
                version=VROPS_VERSION,
                impl_id=VROPS_IMPL_ID,
                group_key=group_key,
                name=name,
                when_to_use=when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group_key] = group_row.id

        for op_id, group_key in _VROPS_SEED_OPS:
            method, path = op_id.split(":", 1)
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=VROPS_PRODUCT,
                version=VROPS_VERSION,
                impl_id=VROPS_IMPL_ID,
                op_id=op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[group_key],
                summary=f"vROps ingested read op {op_id}.",
                description=f"vROps ingested read op {op_id}.",
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=["spec:vcf-operations-9.0/suite-api.yaml"],
            )
            session.add(descriptor)
        await session.commit()


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` for each ``{var}`` in *path*.

    Mirrors :func:`tests.acceptance._nsx_canary_fixtures._param_schema_for`.
    vROps paths carry up to one path variable (``{id}`` on the
    ``vrops.resource.get`` op).
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


async def _vrops_credentials_loader(
    _target: VcfOperationsTargetLike, _operator: Operator
) -> dict[str, str]:
    """Stub credentials loader — bypasses the not-yet-wired Vault read.

    The respx routes accept any Basic-auth header, so the values are
    illustrative. Mirrors the same pattern
    :func:`tests.acceptance._harbor_canary_fixtures._harbor_credentials_loader`
    uses for Harbor.
    """
    return {"username": "vrops-canary-svc", "password": "vrops-canary-pw"}


def _register_vrops_routes(mock: respx.MockRouter) -> None:
    """Register the 8 vROps read-op routes on *mock*.

    vROps uses HTTP Basic on every request — no session establish
    call is needed. Each route returns a pre-seeded JSON body. The
    one templated path (``/suite-api/api/resources/{id}``) is
    registered for the specific id the smoke test dispatches
    against.
    """
    mock.get("/suite-api/api/versions/current").respond(200, json=VROPS_CANARY_VERSION)
    mock.get("/suite-api/api/resources").respond(200, json=VROPS_CANARY_RESOURCES)
    mock.get("/suite-api/api/resources/00000000-0000-4000-8000-000000000000").respond(
        200, json=VROPS_CANARY_RESOURCE_DETAIL
    )
    mock.get("/suite-api/api/alerts").respond(200, json=VROPS_CANARY_ALERTS)
    mock.get("/suite-api/api/alertdefinitions").respond(200, json=VROPS_CANARY_ALERTDEFS)
    mock.get("/suite-api/api/symptoms").respond(200, json=VROPS_CANARY_SYMPTOMS)
    mock.get("/suite-api/api/recommendations").respond(200, json=VROPS_CANARY_RECOMMENDATIONS)
    mock.get("/suite-api/api/supermetrics").respond(200, json=VROPS_CANARY_SUPERMETRICS)


@pytest.fixture
def vrops_acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the vROps dispatch tests act as."""
    return Operator(
        sub="g36-vrops-acceptance",
        name="G3.6-T2 vROps Acceptance",
        email=None,
        raw_jwt="<vrops-acceptance-raw-jwt>",
        tenant_id=VROPS_CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture
async def ingested_vrops_canary(
    pg_engine: None,
    vrops_acceptance_operator: Operator,
) -> AsyncIterator[IngestedVropsCanary]:
    """Yield a dispatcher-ready vROps setup over a respx-mocked appliance.

    Setup mirrors :func:`tests.acceptance._harbor_canary_fixtures.ingested_harbor_canary`:

    1. Insert built-in :class:`OperationGroup` + :class:`EndpointDescriptor`
       rows for the 8 curated vROps core ops.
    2. Seed a :class:`Target` with ``product="vrops"`` and the
       :data:`VROPS_CANARY_FINGERPRINT` so the resolver binds
       :class:`VcfOperationsConnector`.
    3. Resolve + cache the :class:`VcfOperationsConnector` instance the
       dispatcher will use, patching only its ``_creds._loader`` so no
       Vault read fires.
    4. Activate a respx router for :data:`VROPS_CANARY_BASE_URL` and
       register the vROps REST surface.
    """
    await _insert_vrops_descriptors()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=VROPS_CANARY_OPERATOR_TENANT,
            name=VROPS_TARGET_NAME,
            aliases=[],
            # Target.product matches the connector class's registry key
            # (``"vrops"``), not the parse_connector_id-derived
            # ``"vrops"`` the descriptor / group rows use. See module
            # docstring "EndpointDescriptor.product vs Target.product".
            product=_TARGET_PRODUCT,
            host=VROPS_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="vrops/vrops-canary",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=VROPS_CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._vrops_canary_fixtures.ingested_vrops_canary",
        )
        session.add(target)
        await session.commit()

    registry = all_connectors_v2()
    connector_cls = registry.get((_TARGET_PRODUCT, VROPS_VERSION, VROPS_IMPL_ID))
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.vcf_operations as _vrops_pkg

        importlib.reload(_vrops_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get((_TARGET_PRODUCT, VROPS_VERSION, VROPS_IMPL_ID))

    assert connector_cls is VcfOperationsConnector, (
        f"expected VcfOperationsConnector registered for "
        f"({_TARGET_PRODUCT}, {VROPS_VERSION}, {VROPS_IMPL_ID}); "
        f"got {connector_cls!r}"
    )

    instance = get_or_create_connector_instance(connector_cls)
    # Patch the shared CredentialsCache's loader so no Vault read fires.
    # The cache itself is preserved (its lock + cache dict survive across
    # tests) and the next ``get(target)`` call lifts the stub credentials.
    instance._creds._loader = _vrops_credentials_loader  # type: ignore[attr-defined]

    async with respx.mock(
        base_url=VROPS_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_vrops_routes(mock)
        try:
            yield IngestedVropsCanary(
                operator=vrops_acceptance_operator,
                connector_id=VROPS_CONNECTOR_ID,
                target_name=VROPS_TARGET_NAME,
                base_url=VROPS_CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()
