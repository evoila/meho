# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.5 NSX dispatch tests.

Two NSX acceptance modules (dispatch smoke + JSONFlux force-handle)
share the same plumbing: a registered
:class:`~meho_backplane.connectors.nsx.NsxConnector` instance with a
stub session loader (so no Vault read is required), a probed
:class:`~meho_backplane.db.models.Target` row, the ingested browse-breadth
:class:`~meho_backplane.db.models.EndpointDescriptor` rows from
:data:`_NSX_SEED_OPS`, and a :mod:`respx`-mocked NSX REST surface answering
both the session-establish call and every read op the connector dispatches
against.

Why a minimal direct-insert path (not the full G0.7 canary ingest)
==================================================================

The full two-spec NSX ingestion via :func:`IngestionPipelineService`
needs the live NSX ``policy.yaml`` + ``manager.yaml`` (~30 MB
combined) reachable on the CI runner — the same env-gated pattern
:mod:`tests.acceptance._vcenter_spec` codifies for vSphere. That
live two-spec acceptance test is a follow-up to this Task (see the
PR body for #614); until the NSX specs are wired to the meho-runners
pool, the dispatch leg can still be exercised against a minimal
direct-insert path.

This module inserts the ingested NSX browse-breadth
:class:`EndpointDescriptor` rows with hand-authored ``method`` /
``path`` triples (from :data:`_NSX_SEED_OPS`),
seeds one enabled :class:`OperationGroup` per group_key,
opens a respx router for the canary's NSX manager URL, and yields a
small :class:`IngestedNsxCanary` bundle the dispatch tests consume.

Mirrors :mod:`tests.acceptance._canary_fixtures` for the
``vmware-rest`` connector verbatim — same lifecycle, same respx
``assert_all_called=False`` posture, same per-test connector cache
reset on teardown. The NSX-specific delta is the session-create +
XSRF-token-paired auth dance:

* :func:`_register_nsx_routes` answers ``POST /api/session/create``
  with a 200 carrying both the ``X-XSRF-TOKEN`` response header and
  a ``Set-Cookie: JSESSIONID=...`` so the connector's per-target
  httpx client jar picks the cookie up.
* The NSX connector instance's ``_session_loader`` is patched to
  return a static ``{"username": ..., "password": ...}`` pair so no
  Vault read fires.
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
from meho_backplane.connectors.nsx import (
    NSX_CONNECTOR_ID,
    NSX_IMPL_ID,
    NSX_PRODUCT,
    NSX_VERSION,
    NsxConnector,
)
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

# Path-template variable matcher; mirrors the regex
# :mod:`meho_backplane.operations._branches` uses to split a
# templated path. Lifted as a module-internal so the descriptor
# seeder can derive a parameter_schema from the path verbatim.
_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

__all__ = [
    "NSX_CANARY_BASE_URL",
    "NSX_CANARY_CORE_OP_IDS",
    "NSX_CANARY_FINGERPRINT",
    "NSX_CANARY_OPERATOR_TENANT",
    "NSX_CANARY_SEGMENTS",
    "NSX_CANARY_TIER0S",
    "NSX_CANARY_TIER1S",
    "NSX_CANARY_TRANSPORT_NODES",
    "NSX_FORCE_HANDLE_LIST_OP_ID",
    "NSX_TARGET_NAME",
    "IngestedNsxCanary",
    "ingested_nsx_canary",
    "nsx_acceptance_operator",
]

#: Tenant the NSX dispatch tests act under. ``tenant_admin``-scoped
#: operator; the descriptor + group rows themselves stay built-in
#: (``tenant_id=None``) — production NSX content ships as built-in.
NSX_CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000ff")

#: Stable :class:`Target.name` the seeded NSX target carries. Tests
#: refer to it through :attr:`IngestedNsxCanary.target_name`.
NSX_TARGET_NAME: str = "nsx-acceptance"

#: ``.test.invalid`` (RFC 6761 reserved) so no real network egress
#: fires even if respx's transport patching ever regressed. Port 443
#: keeps ``HttpConnector._base_url`` from appending a ``:port``
#: suffix, so the respx ``base_url`` matches the connector's client
#: URL exactly.
NSX_CANARY_BASE_URL: str = "https://nsx-canary.test.invalid"

#: Persisted as ``Target.fingerprint`` — what the connector resolver
#: reads to bind the target's ``product`` + ``version`` against the
#: ``NsxConnector.supported_version_range`` advertisement (``>=4.0,<10.0``).
#: The ``build`` mirrors a VCF-9 appliance's NSX 9.x report (#1530);
#: the probe route normally writes this dict at first-probe time, the
#: dispatch tests seed it directly so the resolver binds the connector
#: without a real probe round-trip.
NSX_CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="vmware",
    product="nsx",
    version=NSX_VERSION,
    build="9.0.2.0.0",
    reachable=True,
    probed_at=datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
    probe_method="GET /api/v1/node",
    extras={"node_uuid": "deadbeef-1111-2222-3333-cafebabecafe"},
).model_dump(mode="json")

#: The list op the JSONFlux force-handle test dispatches. Picked
#: because (a) it's a list op, (b) the segment listing is the
#: largest of the 9 NSX core surfaces in real deployments (often
#: hundreds of rows), and (c) the path requires no template
#: substitution (unlike the firewall-rule list which needs both
#: ``domain-id`` and ``security-policy-id``).
NSX_FORCE_HANDLE_LIST_OP_ID: str = "GET:/policy/api/v1/infra/segments"

#: Synthetic transport-node response — three nodes mimic a 3-host
#: ESXi cluster prepared for NSX overlay.
NSX_CANARY_TRANSPORT_NODES: dict[str, object] = {
    "results": [
        {
            "id": f"transport-node-{i}",
            "display_name": f"esx-canary-{i:02d}",
            "node_deployment_info": {"resource_type": "EsxiNode"},
            "host_switch_spec": {"host_switches": []},
        }
        for i in range(3)
    ],
    "result_count": 3,
}

#: Synthetic segment listing — 12 rows so the force-handle reducer
#: sees a populated set with a sample-row slice.
NSX_CANARY_SEGMENTS: dict[str, object] = {
    "results": [
        {
            "id": f"seg-canary-{i}",
            "display_name": f"canary-seg-{i:02d}",
            "transport_zone_path": (
                "/infra/sites/default/enforcement-points/default/transport-zones/tz-overlay"
            ),
            "subnets": [{"gateway_address": f"10.{i}.0.1/24"}],
            "resource_type": "Segment",
        }
        for i in range(12)
    ],
    "result_count": 12,
}

#: Synthetic tier-0 list — one provider edge.
NSX_CANARY_TIER0S: dict[str, object] = {
    "results": [
        {
            "id": "t0-canary-edge",
            "display_name": "canary-tier0",
            "ha_mode": "ACTIVE_STANDBY",
            "failover_mode": "PREEMPTIVE",
            "resource_type": "Tier0",
        }
    ],
    "result_count": 1,
}

#: Synthetic tier-1 list — two per-tenant routers attached to the
#: tier-0 above.
NSX_CANARY_TIER1S: dict[str, object] = {
    "results": [
        {
            "id": f"t1-canary-{i}",
            "display_name": f"canary-tier1-{i:02d}",
            "tier0_path": "/infra/tier-0s/t0-canary-edge",
            "ha_mode": "ACTIVE_STANDBY",
            "resource_type": "Tier1",
        }
        for i in range(2)
    ],
    "result_count": 2,
}


@dataclass(frozen=True)
class IngestedNsxCanary:
    """Bundle returned by :func:`ingested_nsx_canary`.

    Same shape as :class:`tests.acceptance._canary_fixtures.IngestedCanaryVcsim`
    so the assertion patterns transfer over verbatim.
    """

    operator: Operator
    connector_id: str
    target_name: str
    base_url: str


#: Ingested browse-breadth seed data for the NSX dispatch canary — the five
#: ``source_kind="ingested"`` read ops (and their four groups) declined from
#: typed conversion on #2302 but kept browsable. Relocated here from the
#: retired ``nsx.core_ops`` curation apparatus (#2358): this is test-only
#: fixture material describing the ``EndpointDescriptor`` rows the dispatch
#: tests seed and mock. ``(group_key, name, when_to_use)``.
_NSX_SEED_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("manager-transport-nodes", "NSX Transport Nodes", "Transport-node fabric inventory."),
    ("policy-segments", "NSX Segments", "Policy overlay/VLAN segments."),
    ("policy-tier0", "NSX Tier-0 Gateways", "Tier-0 gateway inventory."),
    (
        "policy-firewall",
        "NSX Distributed Firewall",
        "Distributed-firewall security policies + rules.",
    ),
)

#: ``(op_id, group_key)`` for each ingested browse-breadth NSX read op. Two ops
#: share the ``policy-firewall`` group (security-policies + their rules).
_NSX_SEED_OPS: tuple[tuple[str, str], ...] = (
    ("GET:/api/v1/transport-nodes", "manager-transport-nodes"),
    ("GET:/policy/api/v1/infra/segments", "policy-segments"),
    ("GET:/policy/api/v1/infra/tier-0s", "policy-tier0"),
    ("GET:/policy/api/v1/infra/domains/{domain-id}/security-policies", "policy-firewall"),
    (
        "GET:/policy/api/v1/infra/domains/{domain-id}/security-policies/{security-policy-id}/rules",
        "policy-firewall",
    ),
)

#: Op ids the NSX dispatch/e2e/smoke tests parametrize over (relocated from
#: ``tuple(op.op_id for op in NSX_CORE_OPS)``).
NSX_CANARY_CORE_OP_IDS: tuple[str, ...] = tuple(op_id for op_id, _ in _NSX_SEED_OPS)


async def _insert_nsx_descriptors() -> None:
    """Seed the ingested NSX browse-breadth ops + their groups as enabled rows.

    One :class:`OperationGroup` per entry in :data:`_NSX_SEED_GROUPS`
    (``review_status='enabled'``), one :class:`EndpointDescriptor`
    per entry in :data:`_NSX_SEED_OPS`
    (``is_enabled=True``, ``source_kind='ingested'``, ``handler_ref=None``).

    Two ops in :data:`_NSX_SEED_OPS` reference the same ``policy-firewall``
    group; the helper coalesces those into one inserted group row so
    the FK to ``operation_group.id`` resolves correctly on both ops.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group_key, name, when_to_use in _NSX_SEED_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=NSX_PRODUCT,
                version=NSX_VERSION,
                impl_id=NSX_IMPL_ID,
                group_key=group_key,
                name=name,
                when_to_use=when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group_key] = group_row.id

        for op_id, group_key in _NSX_SEED_OPS:
            method, path = op_id.split(":", 1)
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=NSX_PRODUCT,
                version=NSX_VERSION,
                impl_id=NSX_IMPL_ID,
                op_id=op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[group_key],
                summary=f"NSX ingested read op {op_id}.",
                description=f"NSX ingested read op {op_id}.",
                # ``x-meho-param-loc='path'`` declares which params the
                # dispatcher's ``_split_ingested_params`` routes to URL
                # template substitution. The G0.7 ingestion pipeline
                # writes this extension key off the OpenAPI ``in: path``
                # parameter declarations; the seeded descriptors mirror
                # that shape so the firewall-policy / firewall-rule ops
                # substitute their ``{domain-id}`` /
                # ``{security-policy-id}`` placeholders correctly.
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=[_spec_tag_for(path)],
            )
            session.add(descriptor)
        await session.commit()


def _spec_tag_for(path: str) -> str:
    """Return the ``spec:<source>`` tag matching the path's upstream NSX spec.

    NSX's OpenAPI corpus is split across two files:

    * ``policy.yaml`` — the modern declarative policy API; every
      path begins with ``/policy/api/v1/...``.
    * ``manager.yaml`` — the legacy / lower-level manager API; every
      path begins with ``/api/v1/...``.

    The G0.7 ingestion pipeline tags every persisted row with
    ``spec:<source>`` so operators can audit per-spec coverage via
    ``meho connector review``. The seeded fixture mirrors that
    contract — the 6 policy-API ops carry ``spec:nsx-9.0/policy.yaml``,
    the 3 manager-API ops carry ``spec:nsx-9.0/manager.yaml`` (the
    VCF-9-aligned line, #1530). A flat single-tag default would
    misrepresent the corpus split and drift the dispatch-smoke set
    away from what a real two-spec ingest would land.
    """
    if path.startswith("/policy/"):
        return "spec:nsx-9.0/policy.yaml"
    return "spec:nsx-9.0/manager.yaml"


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` declaring every ``{var}`` as a path param.

    Returns the canonical OpenAPI-flavoured shape the G0.7 ingestion
    pipeline produces for path-templated ops: an object schema with
    each ``{name}`` placeholder declared as a property carrying
    ``x-meho-param-loc='path'``. Non-templated paths get the empty
    ``{"type": "object", "properties": {}}`` shape every other
    ingested op uses.

    Lifted out of :func:`_insert_nsx_descriptors` so the descriptor
    loop stays focused on row construction and the schema-shape rule
    has one obvious location to read off.
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


async def _nsx_session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Stub session loader — bypasses the live operator-context Vault read.

    The respx ``POST /api/session/create`` route accepts any pair, so
    the values are illustrative. Mirrors the same pattern
    :func:`tests.acceptance._canary_fixtures._vcenter_rest_session_loader`
    uses for vSphere; the ``_operator`` argument matches the 2-arg
    :class:`~meho_backplane.connectors.nsx.session.NsxSessionLoader`
    signature G3.10-T1 (#945) introduced.
    """
    return {"username": "nsx-canary-svc", "password": "nsx-canary-pw"}


def _register_nsx_routes(mock: respx.MockRouter) -> None:
    """Register the NSX session-establish + 9 read-op routes on *mock*.

    The session-create route answers with both an ``X-XSRF-TOKEN``
    header and a ``Set-Cookie`` (which httpx's per-target client jar
    captures automatically). Every subsequent route returns a
    pre-seeded JSON body matching the rough shape NSX returns for
    that path family.

    ``assert_all_called=False`` on the fixture's router means a test
    that exercises only one op doesn't trip on the unused routes;
    ``assert_all_mocked=False`` would let an unmatched request fall
    through to the real network (we don't enable that here — NSX
    dispatch tests don't need an out-of-band download path like the
    fastembed model fetch the vSphere agent-flow test triggers).
    """
    # Session establish. NSX returns 200 with the JSESSIONID
    # cookie + X-XSRF-TOKEN header; the body is empty.
    mock.post("/api/session/create").respond(
        200,
        headers={
            "X-XSRF-TOKEN": "canary-xsrf-token",
            "Set-Cookie": "JSESSIONID=canary-session-id; Path=/; HttpOnly",
        },
    )
    # nsx.about — manager identity probe.
    mock.get("/api/v1/node").respond(
        200,
        json={
            "node_version": NSX_VERSION,
            "kernel_version": "9.0.2.0.0",
            "node_uuid": "deadbeef-1111-2222-3333-cafebabecafe",
            "hostname": "nsxmgr-canary",
            "external_id": "canary-external-id",
        },
    )
    # nsx.node.list — transport-node inventory.
    mock.get("/api/v1/transport-nodes").respond(200, json=NSX_CANARY_TRANSPORT_NODES)
    # nsx.cluster.status — manager cluster health.
    mock.get("/api/v1/cluster/status").respond(
        200,
        json={
            "mgmt_cluster_status": {"status": "STABLE"},
            "control_cluster_status": {"status": "STABLE"},
            "detail": [{"member_uuid": "deadbeef-1111", "status": "CONNECTED"}],
        },
    )
    # nsx.segment.list — policy-API segments.
    mock.get("/policy/api/v1/infra/segments").respond(200, json=NSX_CANARY_SEGMENTS)
    # nsx.transport_zone.list — under the default enforcement point.
    mock.get(
        "/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones"
    ).respond(
        200,
        json={
            "results": [
                {
                    "id": "tz-overlay",
                    "display_name": "canary-overlay-tz",
                    "tz_type": "OVERLAY",
                    "host_switch_name": "canary-host-switch",
                    "resource_type": "PolicyTransportZone",
                }
            ],
            "result_count": 1,
        },
    )
    # nsx.tier0.list / nsx.tier1.list — policy-API edge + tenant routers.
    mock.get("/policy/api/v1/infra/tier-0s").respond(200, json=NSX_CANARY_TIER0S)
    mock.get("/policy/api/v1/infra/tier-1s").respond(200, json=NSX_CANARY_TIER1S)
    # nsx.firewall.policy.list — security policies under domain ``default``.
    mock.get("/policy/api/v1/infra/domains/default/security-policies").respond(
        200,
        json={
            "results": [
                {
                    "id": "policy-app-tier",
                    "display_name": "canary-app-policy",
                    "category": "Application",
                    "sequence_number": 100,
                    "scope": ["ANY"],
                    "resource_type": "SecurityPolicy",
                }
            ],
            "result_count": 1,
        },
    )
    # nsx.firewall.rule.list — per-policy rules.
    mock.get(
        "/policy/api/v1/infra/domains/default/security-policies/policy-app-tier/rules"
    ).respond(
        200,
        json={
            "results": [
                {
                    "id": "rule-1",
                    "display_name": "canary-allow-http",
                    "action": "ALLOW",
                    "sources": ["ANY"],
                    "destinations": ["ANY"],
                    "services": ["HTTP"],
                    "applied_to": ["ANY"],
                    "resource_type": "Rule",
                }
            ],
            "result_count": 1,
        },
    )


@pytest.fixture
def nsx_acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the NSX dispatch tests act as.

    ``tenant_admin`` role so the dispatcher's tenant-scoped queries
    succeed against built-in (``tenant_id=None``) descriptor rows;
    matches the canary's ``acceptance_operator`` shape verbatim.
    """
    return Operator(
        sub="g35-nsx-acceptance",
        name="G3.5-T2 NSX Acceptance",
        email=None,
        raw_jwt="<nsx-acceptance-raw-jwt>",
        tenant_id=NSX_CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture
async def ingested_nsx_canary(
    pg_engine: None,
    nsx_acceptance_operator: Operator,
) -> AsyncIterator[IngestedNsxCanary]:
    """Yield a dispatcher-ready NSX setup over a respx-mocked manager.

    Setup steps mirror :func:`tests.acceptance._canary_fixtures.ingested_canary_vcsim`:

    1. Insert built-in :class:`OperationGroup` + :class:`EndpointDescriptor`
       rows for the curated NSX core ops.
    2. Seed a :class:`Target` carrying the :data:`NSX_CANARY_FINGERPRINT`
       so the resolver binds :class:`NsxConnector` (version 9.0 fits
       its ``">=4.0,<10.0"`` advertisement).
    3. Resolve + cache the :class:`NsxConnector` instance the
       dispatcher will use, patching only its ``_session_loader``.
       The httpx client is **not** patched — respx intercepts the
       transport.
    4. Activate a respx router for :data:`NSX_CANARY_BASE_URL` and
       register the NSX REST surface.

    Teardown (inside the active respx router so the session-revoke
    side-effects in any future ``aclose`` flow are intercepted):

    1. ``aclose()`` the connector instance.
    2. :func:`reset_dispatcher_caches` so the next test sees a fresh
       instance.

    The :class:`NsxConnector` registration survives across tests
    under normal operation; the fixture re-imports the ``nsx``
    package on demand if a sibling test wiped the v2 registry (the
    G0.7 canary's autouse cleanup does this).
    """
    await _insert_nsx_descriptors()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=NSX_CANARY_OPERATOR_TENANT,
            name=NSX_TARGET_NAME,
            aliases=[],
            product=NSX_PRODUCT,
            host=NSX_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="nsx/nsx-canary",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=NSX_CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._nsx_canary_fixtures.ingested_nsx_canary",
        )
        session.add(target)
        await session.commit()

    # Resolve the connector class from the v2 registry. The nsx
    # package registers itself at module import time; the
    # registration survives across tests unless something explicitly
    # clears it. Mirror the vSphere fixture's re-import fallback for
    # tests that ran ``clear_registry()``.
    registry = all_connectors_v2()
    connector_cls = registry.get((NSX_PRODUCT, NSX_VERSION, NSX_IMPL_ID))
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.nsx as _nsx_pkg

        importlib.reload(_nsx_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get((NSX_PRODUCT, NSX_VERSION, NSX_IMPL_ID))

    assert connector_cls is NsxConnector, (
        f"expected NsxConnector registered for "
        f"({NSX_PRODUCT}, {NSX_VERSION}, {NSX_IMPL_ID}); got {connector_cls!r}"
    )

    # Materialise the cached instance the dispatcher will use; replace
    # only the Vault-backed session loader. respx intercepts the
    # connector's own httpx client at the transport layer.
    instance = get_or_create_connector_instance(connector_cls)
    instance._session_loader = _nsx_session_loader  # type: ignore[attr-defined]

    async with respx.mock(
        base_url=NSX_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_nsx_routes(mock)
        try:
            yield IngestedNsxCanary(
                operator=nsx_acceptance_operator,
                connector_id=NSX_CONNECTOR_ID,
                target_name=NSX_TARGET_NAME,
                base_url=NSX_CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()
