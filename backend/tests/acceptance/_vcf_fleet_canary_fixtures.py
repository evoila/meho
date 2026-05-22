# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.6-T9 VCF Fleet dispatch tests.

The E2E module (:mod:`tests.test_connectors_vcf_fleet_e2e`) needs a
registered :class:`~meho_backplane.connectors.vcf_fleet.VcfFleetConnector`
instance with a stub credentials loader (so no Vault read fires), a
probed :class:`~meho_backplane.db.models.Target`, the 8 curated
:class:`~meho_backplane.db.models.EndpointDescriptor` rows from
:data:`~meho_backplane.connectors.vcf_fleet.core_ops.FLEET_CORE_OPS`,
and a :mod:`respx`-mocked Fleet REST surface answering each of the 8
curated read ops.

Fleet uses HTTP Basic auth against the local LCM user store on every
request — no session-establish call, no XSRF-token dance, no 401-retry
loop. The stub credentials loader bypasses the Vault-backed loader; the
respx router matches by method + path (Basic auth header is not asserted
by default, matching the SDDC Manager precedent).

Why a minimal direct-insert path (not full G0.7 canary ingest)
==============================================================

VCF Fleet has no public CI simulator (Initiative #369 DoD), and the
full vRSLCM OpenAPI ingest needs the spec file reachable on the runner.
Until the spec-shelf is wired to the meho-runners pool, the dispatch
leg is exercised against a direct-insert path that seeds the 8 curated
endpoint_descriptor rows by hand. Same pattern :mod:`tests.acceptance._nsx_canary_fixtures`
and :mod:`tests.acceptance._sddc_canary_fixtures` established for the
other no-public-simulator connectors.

``EndpointDescriptor.product`` note
===================================

Rows are inserted with ``product=FLEET_PRODUCT="fleet"`` — the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` derives
from ``"fleet-rest-9.0"`` (the first hyphen-segment of impl_id). The
:class:`Target` row uses ``product="vcf-fleet"`` so the resolver finds
:class:`VcfFleetConnector` (registered with ``product="vcf-fleet"`` in
the v2 registry). These product values serve different purposes; both
are required for end-to-end dispatch to succeed. Same divergence
:mod:`tests.acceptance._sddc_canary_fixtures` documents for SDDC
Manager.

Fixture payloads are JSON-coercible Python literals mirroring the
shapes the recorded-fixture refresh tool
(``backend/tests/fixtures/vcf/refresh.py``) records from a real
appliance — the on-disk JSON files live under
``backend/tests/fixtures/vcf/vcf-fleet/`` once an operator runs the
refresh script. This module's literals exist so the in-process tests
don't depend on those files being present; the refresh tool documents
the recording flow so a future maintainer can re-record after an
appliance upgrade.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.vcf_fleet import (
    FLEET_CORE_GROUPS,
    FLEET_CORE_OPS,
    FLEET_IMPL_ID,
    FLEET_PRODUCT,
    FLEET_VERSION,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup

_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

__all__ = [
    "FLEET_CANARY_BASE_URL",
    "FLEET_CANARY_DATACENTERS",
    "FLEET_CANARY_ENVIRONMENTS",
    "FLEET_CANARY_FINGERPRINT",
    "FLEET_CANARY_OPERATOR_TENANT",
    "FLEET_CANARY_PRODUCTS",
    "FLEET_CANARY_REQUESTS",
    "FLEET_CANARY_VCENTERS",
    "FLEET_FORCE_HANDLE_LIST_OP_ID",
    "FLEET_TARGET_NAME",
    "_fleet_credentials_loader",
    "_insert_fleet_descriptors",
    "_register_fleet_routes",
]

#: Tenant the VCF Fleet dispatch tests act under. Distinct UUID per
#: connector family to avoid cross-test interference if the per-test
#: SQLite DB ever becomes shared.
FLEET_CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000fd")

#: Stable :class:`Target.name` for the seeded Fleet target.
FLEET_TARGET_NAME: str = "fleet-acceptance"

#: ``.test.invalid`` (RFC 6761 reserved) so no real network egress
#: fires. Port 443 keeps ``HttpConnector._base_url`` from appending a
#: ``:port`` suffix, so the respx ``base_url`` matches the connector's
#: client URL exactly.
FLEET_CANARY_BASE_URL: str = "https://fleet-canary.test.invalid"

#: The list op the JSONFlux force-handle test dispatches. Environment
#: listing is the largest curated Fleet read surface in real
#: deployments — every vRA / vROps / vRLI / vIDM deploy on the appliance
#: lives under an environment, and busy Fleets manage dozens. Mirrors
#: the NSX (segment list) and SDDC Manager (host list) choices.
FLEET_FORCE_HANDLE_LIST_OP_ID: str = "GET:/lcm/lcops/api/v2/environments"

#: Persisted as ``Target.fingerprint`` so the resolver binds
#: :class:`VcfFleetConnector` (``supported_version_range=">=9.0,<10.0"``).
#: The probe normally writes this dict at first-probe time; the dispatch
#: tests seed it directly so the resolver binds without a real probe
#: round-trip.
#:
#: The persisted ``version`` is the connector class's declared product
#: version (``FLEET_VERSION = "9.0"``) so the resolver's
#: ``supported_version_range`` filter matches. This differs from what
#: :meth:`VcfFleetConnector.fingerprint` writes at runtime — that method
#: stores the ``Lcm-API-Version`` response header (e.g. ``"8.0"``) under
#: ``version`` because Fleet exposes no working product-version endpoint
#: in 9.0. The seeded canary uses the product version to satisfy the
#: resolver's contract; the runtime-observable LCM API header is carried
#: under ``extras.lcm_api_version`` for documentation.
FLEET_CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="vmware",
    product="vcf-fleet",
    version=FLEET_VERSION,
    build=None,
    reachable=True,
    probed_at=datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC),
    probe_method=(
        "GET /lcm/lcops/api/v2/datacenters with HTTP Basic; read Lcm-API-Version response header"
    ),
    extras={
        "lcm_api_version": "8.0",
        "datacenter_count": 2,
        "product_lineage": "vmware-vrealize-suite-lifecycle-manager",
        "diagnostic_endpoints_broken": [
            "/lcm/lcops/api/v2/about",
            "/lcm/lcops/api/v2/health",
            "/lcm/lcops/api/v2/version",
            "/lcm/lcops/api/v2/system-details",
            "/lcm/common/api/about",
            "/lcm/locker/api/v2/about",
        ],
    },
).model_dump(mode="json")

#: Synthetic Fleet ``about`` response. Note: in real VCF 9.0 builds the
#: appliance returns HTTP 500 on this endpoint; the canary returns a
#: well-formed payload so the dispatch test can exercise the parse path
#: too. The connector's E2E `about` row exists for spec parity — the
#: known-issue documentation lives in the curated `llm_instructions`.
FLEET_CANARY_ABOUT: dict[str, object] = {
    "apiVersion": "8.0",
    "productVersion": "9.0.0.0",
    "buildNumber": "24123456",
    "releaseDate": "2026-04-01",
}

#: Synthetic datacenter list — two entries (the wrapper-verified probe
#: payload).
FLEET_CANARY_DATACENTERS: list[dict[str, object]] = [
    {
        "vmid": "dc-canary-001",
        "name": "primary",
        "type": "PRIVATE_CLOUD",
        "city": "Vienna",
        "country": "AT",
        "vCenters": [],
    },
    {
        "vmid": "dc-canary-002",
        "name": "secondary",
        "type": "PRIVATE_CLOUD",
        "city": "Frankfurt",
        "country": "DE",
        "vCenters": [],
    },
]

#: Synthetic vCenter list for the ``dc-canary-001`` datacenter.
FLEET_CANARY_VCENTERS: list[dict[str, object]] = [
    {
        "vmid": "vc-canary-001",
        "name": "vc-prod",
        "hostname": "vc-prod.lab.example.com",
        "buildNumber": "24001122",
        "version": "8.0.3",
        "dataCollectionStatus": {
            "status": "COMPLETED",
            "lastDataCollectionTime": "2026-05-21T18:00:00Z",
        },
    },
]

#: Synthetic environment list — 8 entries so the force-handle reducer
#: sees a populated set with a sample-row slice.
FLEET_CANARY_ENVIRONMENTS: list[dict[str, object]] = [
    {
        "environmentId": f"env-canary-{i:03d}",
        "environmentName": f"env-canary-{i:03d}",
        "environmentDescription": f"Canary environment {i}",
        "environmentStatus": "DEPLOY_SUCCESSFUL",
        "products": [{"productId": "vrops" if i % 2 == 0 else "vrli", "version": "9.0.0"}],
    }
    for i in range(8)
]

#: Synthetic detail for ``env-canary-000``.
FLEET_CANARY_ENVIRONMENT_DETAIL: dict[str, object] = {
    "environmentId": "env-canary-000",
    "environmentName": "env-canary-000",
    "environmentStatus": "DEPLOY_SUCCESSFUL",
    "transactionId": "txn-canary-001",
    "createdOn": "2026-05-01T12:00:00Z",
    "modifiedOn": "2026-05-20T15:00:00Z",
    "products": [
        {
            "productId": "vrops",
            "version": "9.0.0",
            "status": "DEPLOY_SUCCESSFUL",
            "nodes": [
                {
                    "hostname": "vrops-master.lab.example.com",
                    "ipAddress": "10.1.1.10",
                    "role": "MASTER",
                    "vmStatus": "POWERED_ON",
                }
            ],
        }
    ],
}

#: Synthetic product list for ``env-canary-000``.
FLEET_CANARY_PRODUCTS: list[dict[str, object]] = [
    {
        "productId": "vrops",
        "version": "9.0.0",
        "status": "DEPLOY_SUCCESSFUL",
        "nodes": [
            {
                "hostname": "vrops-master.lab.example.com",
                "ipAddress": "10.1.1.10",
                "role": "MASTER",
                "vmStatus": "POWERED_ON",
            }
        ],
    }
]

#: Synthetic request list — three requests, one in-flight, two completed.
FLEET_CANARY_REQUESTS: list[dict[str, object]] = [
    {
        "vmid": "req-canary-001",
        "requestName": "Create environment env-canary-000",
        "requestType": "ENVIRONMENT_CREATE",
        "state": "COMPLETED",
        "createdOn": "2026-05-01T11:00:00Z",
        "lastUpdatedOn": "2026-05-01T12:00:00Z",
    },
    {
        "vmid": "req-canary-002",
        "requestName": "Upgrade vROps to 9.0.1",
        "requestType": "PRODUCT_UPGRADE",
        "state": "INPROGRESS",
        "createdOn": "2026-05-22T08:00:00Z",
        "lastUpdatedOn": "2026-05-22T08:30:00Z",
    },
    {
        "vmid": "req-canary-003",
        "requestName": "Patch vRLI to 9.0.2",
        "requestType": "PRODUCT_PATCH",
        "state": "COMPLETED",
        "createdOn": "2026-05-15T09:00:00Z",
        "lastUpdatedOn": "2026-05-15T10:30:00Z",
    },
]

#: Synthetic detail for ``req-canary-002`` (INPROGRESS).
FLEET_CANARY_REQUEST_DETAIL: dict[str, object] = {
    "vmid": "req-canary-002",
    "transactionId": "txn-canary-009",
    "requestName": "Upgrade vROps to 9.0.1",
    "requestReason": "Scheduled upgrade",
    "requestType": "PRODUCT_UPGRADE",
    "requestSource": "OPERATOR",
    "requestSourceType": "API",
    "state": "INPROGRESS",
    "executionId": "exec-canary-009",
    "executionStatus": "RUNNING",
    "executionPath": [
        {"stage": "PRE_VALIDATE", "status": "COMPLETED"},
        {"stage": "UPGRADE", "status": "RUNNING"},
    ],
    "errorCause": "",
    "resultSet": "",
    "isCancelEnabled": True,
    "lastUpdatedOn": "2026-05-22T08:30:00Z",
    "createdBy": "admin@local",
    "inputMap": {"productId": "vrops", "targetVersion": "9.0.1"},
    "outputMap": {},
}


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` declaring every ``{var}`` as a path param.

    Mirrors :func:`tests.acceptance._nsx_canary_fixtures._param_schema_for` /
    :func:`tests.acceptance._sddc_canary_fixtures._param_schema_for`.
    Non-templated paths get the empty ``{"type": "object", "properties": {}}``
    shape every other ingested op uses.
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


async def _insert_fleet_descriptors() -> None:
    """Seed the 8 curated Fleet core ops + their groups as enabled rows.

    One :class:`OperationGroup` per entry in
    :data:`~meho_backplane.connectors.vcf_fleet.FLEET_CORE_GROUPS`
    (``review_status='enabled'``), one :class:`EndpointDescriptor` per
    entry in :data:`~meho_backplane.connectors.vcf_fleet.FLEET_CORE_OPS`
    (``is_enabled=True``, ``source_kind='ingested'``,
    ``handler_ref=None``).

    Rows use ``product=FLEET_PRODUCT="fleet"`` (from
    :func:`parse_connector_id("fleet-rest-9.0")`), not the connector
    class's ``product="vcf-fleet"``. The :class:`Target` row uses
    ``product="vcf-fleet"`` so the resolver finds
    :class:`VcfFleetConnector`.

    Multiple ops can reference the same ``group_key`` — environments
    has two ops (list + info), requests has two ops (list + info) — so
    the helper coalesces group inserts via the ``group_ids`` dict and
    skips re-inserting on the second op encounter.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group in FLEET_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=FLEET_PRODUCT,
                version=FLEET_VERSION,
                impl_id=FLEET_IMPL_ID,
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in FLEET_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=FLEET_PRODUCT,
                version=FLEET_VERSION,
                impl_id=FLEET_IMPL_ID,
                op_id=op.op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[op.group_key],
                summary=f"VCF Fleet core op {op.op_id} (curated read).",
                description=f"VCF Fleet core op {op.op_id} (curated read).",
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=op.llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=["spec:vcf-fleet-9.0/lcm-rest.yaml"],
            )
            session.add(descriptor)
        await session.commit()


async def _fleet_credentials_loader(_target: object) -> dict[str, str]:
    """Stub credentials loader — bypasses the not-yet-wired Vault read.

    Returns illustrative HTTP Basic credentials. The respx routes match
    by path (no auth-header assertion), so the values are placeholders.
    Mirrors :func:`tests.acceptance._sddc_canary_fixtures._sddc_credentials_loader`.
    """
    return {"username": "admin@local", "password": "fleet-canary-pw"}


def _register_fleet_routes(mock: respx.MockRouter) -> None:
    """Register the 8 Fleet read-op routes on *mock*.

    Fleet uses HTTP Basic on every request — no session-establish call.
    Each route returns a pre-seeded JSON body matching the rough shape
    the vRSLCM REST surface returns for that path family.

    Path-templated routes are registered against the specific id the
    E2E test passes as the corresponding parameter (the dispatcher's
    ``_substitute_path`` fills the placeholder before the request
    fires; respx then matches the substituted path).
    """
    # /about — returns the synthetic well-formed payload (the real 9.0
    # appliance returns HTTP 500 here; the canary's well-formed return
    # lets the parser-path test exercise the success branch too).
    mock.get("/lcm/lcops/api/v2/about").respond(200, json=FLEET_CANARY_ABOUT)
    # Datacenters — bare array, no envelope.
    mock.get("/lcm/lcops/api/v2/datacenters").respond(200, json=FLEET_CANARY_DATACENTERS)
    # vCenters under one datacenter — path-templated against dc-canary-001.
    mock.get("/lcm/lcops/api/v2/datacenters/dc-canary-001/vcenters").respond(
        200,
        json=FLEET_CANARY_VCENTERS,
    )
    # Environments — bare array.
    mock.get("/lcm/lcops/api/v2/environments").respond(200, json=FLEET_CANARY_ENVIRONMENTS)
    # Environment detail — path-templated against env-canary-000.
    mock.get("/lcm/lcops/api/v2/environments/env-canary-000").respond(
        200,
        json=FLEET_CANARY_ENVIRONMENT_DETAIL,
    )
    # Products under one environment — path-templated.
    mock.get("/lcm/lcops/api/v2/environments/env-canary-000/products").respond(
        200,
        json=FLEET_CANARY_PRODUCTS,
    )
    # Requests — bare array.
    mock.get("/lcm/request/api/v2/requests").respond(200, json=FLEET_CANARY_REQUESTS)
    # Request detail — path-templated against req-canary-002.
    mock.get("/lcm/request/api/v2/requests/req-canary-002").respond(
        200,
        json=FLEET_CANARY_REQUEST_DETAIL,
    )


def fleet_acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the VCF Fleet dispatch tests act as.

    Function (not a pytest fixture) so the E2E module can compose its
    own operator instance with module-scoped lifetimes if needed.
    """
    return Operator(
        sub="g36-fleet-acceptance",
        name="G3.6-T9 Fleet Acceptance",
        email=None,
        raw_jwt="<fleet-acceptance-raw-jwt>",
        tenant_id=FLEET_CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )
