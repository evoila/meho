# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared minimal-setup fixtures for the G3.7 Hetzner Robot dispatch tests.

Two Robot acceptance modules (dispatch smoke + JSONFlux force-handle)
share the same plumbing: a registered
:class:`~meho_backplane.connectors.hetzner_robot.HetznerRobotConnector` instance
with a stub credentials loader (so no Vault read is required), a probed
:class:`~meho_backplane.db.models.Target` row, the 10 curated
:class:`~meho_backplane.db.models.EndpointDescriptor` rows from
:data:`~meho_backplane.connectors.hetzner_robot.core_ops.ROBOT_CORE_OPS`, and a
:mod:`respx`-mocked Hetzner Robot REST surface answering each of the 10 curated
read ops.

Hetzner Robot uses HTTP Basic auth on every request — no session establish
or XSRF-token dance is needed. The stub credentials loader bypasses the
Vault-backed loader; the respx router matches requests by path.

Why a minimal direct-insert path (not full G0.7 canary ingest)
==============================================================

The full Hetzner Robot spec ingest via :class:`IngestionPipelineService`
needs the Robot OpenAPI spec reachable on the CI runner plus a live LLM
for the grouping pass. Until the spec-shelf is wired to the meho-runners
pool, the dispatch leg is exercised against a minimal direct-insert path
that seeds the 10 curated endpoint_descriptor rows by hand. Same pattern
:mod:`tests.acceptance._harbor_canary_fixtures` and
:mod:`tests.acceptance._sddc_canary_fixtures` established.

``EndpointDescriptor.product`` note
====================================

Rows are inserted with ``product=ROBOT_PRODUCT="hetzner"`` — the value
:func:`~meho_backplane.operations._lookup.parse_connector_id` derives from
``"hetzner-rest-2026.04"`` (first hyphen-segment of impl_id). The
:class:`Target` row also uses ``product="hetzner"`` so the resolver
finds :class:`HetznerRobotConnector` (registered with
``product="hetzner"`` in the v2 registry). Since #1814 (Initiative
#1810) the registry key, the descriptor/group rows, and the
parser-derived token all agree on ``"hetzner"``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.hetzner_robot import (
    ROBOT_CONNECTOR_ID,
    ROBOT_CORE_GROUPS,
    ROBOT_CORE_OPS,
    ROBOT_IMPL_ID,
    ROBOT_PRODUCT,
    ROBOT_VERSION,
    HetznerRobotConnector,
    HetznerRobotTargetLike,
)
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

__all__ = [
    "ROBOT_CANARY_BASE_URL",
    "ROBOT_CANARY_FINGERPRINT",
    "ROBOT_CANARY_OPERATOR_TENANT",
    "ROBOT_CANARY_SERVERS",
    "ROBOT_FORCE_HANDLE_LIST_OP_ID",
    "ROBOT_FORCE_HANDLE_PARAMS",
    "ROBOT_SANDBOX_TARGET_NAME",
    "ROBOT_TARGET_NAME",
    "IngestedRobotCanary",
    "ingested_robot_canary",
    "ingested_robot_canary_sandbox",
    "robot_acceptance_operator",
]

#: Tenant the Robot dispatch tests act under.
ROBOT_CANARY_OPERATOR_TENANT: UUID = UUID("00000000-0000-0000-0000-0000000000fe")

#: Stable :class:`Target.name` for the seeded Robot target.
ROBOT_TARGET_NAME: str = "robot-acceptance"

#: Stable :class:`Target.name` for the seeded sandbox Robot target
#: (all endpoints return HTTP 200 + empty arrays).
ROBOT_SANDBOX_TARGET_NAME: str = "robot-acceptance-sandbox"

#: ``.test.invalid`` (RFC 6761 reserved) so no real network egress fires.
ROBOT_CANARY_BASE_URL: str = "https://robot-canary.test.invalid"

#: Persisted as ``Target.fingerprint`` so the resolver binds
#: :class:`HetznerRobotConnector`.
ROBOT_CANARY_FINGERPRINT: dict[str, object] = FingerprintResult(
    vendor="hetzner",
    product="robot-webservice",
    version=None,
    build=None,
    reachable=True,
    probed_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
    probe_method="GET /server",
    extras={
        "account_id": "100001",
        "server_count": 3,
    },
).model_dump(mode="json")

#: The list op the JSONFlux force-handle test dispatches. Server list is
#: the main inventory surface in a Hetzner Robot account, mirroring the
#: NSX segment-list and Harbor artifact-list choices.
ROBOT_FORCE_HANDLE_LIST_OP_ID: str = "GET:/server"

#: Smoke-test path parameters for ``ROBOT_FORCE_HANDLE_LIST_OP_ID``.
#: ``GET:/server`` takes no path parameters — empty dict is correct.
ROBOT_FORCE_HANDLE_PARAMS: dict[str, str] = {}

#: Synthetic query/account-info response.
ROBOT_CANARY_QUERY: dict[str, object] = {
    "api_version": "1.0",
    "account_id": "robot-canary-account",
}

#: Synthetic server list — 3 dedicated servers.
ROBOT_CANARY_SERVERS: list[dict[str, object]] = [
    {
        "server": {
            "server_ip": f"1.2.3.{i + 1}",
            "server_number": 100000 + i + 1,
            "server_name": f"canary-server-{i + 1}",
            "product": "AX41-NVMe",
            "dc": f"FSN1-DC{i + 14}",
            "traffic": "unlimited",
            "flatrate": True,
            "status": "ready",
            "throttled": False,
            "cancelled": False,
            "paid_until": "2027-01-01",
        }
    }
    for i in range(3)
]

#: Synthetic single-server detail (server-ip=1.2.3.1).
ROBOT_CANARY_SERVER_DETAIL: dict[str, object] = {
    "server": {
        "server_ip": "1.2.3.1",
        "server_number": 100001,
        "server_name": "canary-server-1",
        "product": "AX41-NVMe",
        "dc": "FSN1-DC14",
        "traffic": "unlimited",
        "flatrate": True,
        "status": "ready",
        "throttled": False,
        "cancelled": False,
        "paid_until": "2027-01-01",
    }
}

#: Synthetic IP list — 3 IPs matching the server list.
ROBOT_CANARY_IPS: list[dict[str, object]] = [
    {
        "ip": {
            "ip": f"1.2.3.{i + 1}",
            "server_ip": f"1.2.3.{i + 1}",
            "locked": False,
            "separate_mac": None,
            "traffic_warnings": False,
            "traffic_hourly": None,
            "traffic_daily": None,
            "traffic_monthly": None,
        }
    }
    for i in range(3)
]

#: Synthetic subnet list.
ROBOT_CANARY_SUBNETS: list[dict[str, object]] = [
    {
        "subnet": {
            "ip": "2a01:4f8::/29",
            "mask": "29",
            "gateway": "fe80::1",
            "server_ip": "1.2.3.1",
            "ip_version": 6,
        }
    }
]

#: Synthetic vSwitch list.
ROBOT_CANARY_VSWITCHES: list[dict[str, object]] = [
    {
        "vswitch": {
            "id": 4321,
            "name": "canary-vswitch",
            "vlan": 4000,
            "cancelled": False,
            "server": [
                {"server_ip": "1.2.3.1", "server_number": 100001, "status": "ready"},
            ],
        }
    }
]

#: Synthetic single-vSwitch detail (id=4321).
ROBOT_CANARY_VSWITCH_DETAIL: dict[str, object] = {
    "vswitch": {
        "id": 4321,
        "name": "canary-vswitch",
        "vlan": 4000,
        "cancelled": False,
        "server": [
            {"server_ip": "1.2.3.1", "server_number": 100001, "status": "ready"},
        ],
    }
}

#: Synthetic failover list.
ROBOT_CANARY_FAILOVERS: list[dict[str, object]] = [
    {
        "failover": {
            "ip": "1.2.3.10",
            "netmask": "255.255.255.255",
            "server_ip": "1.2.3.1",
            "active_server_ip": "1.2.3.1",
        }
    }
]

#: Synthetic rDNS list.
ROBOT_CANARY_RDNS: list[dict[str, object]] = [
    {"rdns": {"ip": "1.2.3.1", "ptr": "canary-server-1.example.com"}},
    {"rdns": {"ip": "1.2.3.2", "ptr": "canary-server-2.example.com"}},
]

#: Synthetic SSH key list — 2 keys registered in the Robot portal.
ROBOT_CANARY_KEYS: list[dict[str, object]] = [
    {
        "key": {
            "name": "canary-ed25519",
            "fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
            "type": "ED25519",
            "size": 256,
        }
    },
    {
        "key": {
            "name": "canary-rsa",
            "fingerprint": "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
            "type": "RSA",
            "size": 4096,
        }
    },
]


@dataclass(frozen=True)
class IngestedRobotCanary:
    """Bundle returned by :func:`ingested_robot_canary`."""

    operator: Operator
    connector_id: str
    target_name: str
    base_url: str


async def _insert_robot_descriptors() -> None:
    """Seed the 10 curated Robot core ops + their groups as enabled rows.

    One :class:`OperationGroup` per entry in :data:`ROBOT_CORE_GROUPS`
    (``review_status='enabled'``), one :class:`EndpointDescriptor` per
    entry in :data:`ROBOT_CORE_OPS` (``is_enabled=True``,
    ``source_kind='ingested'``, ``handler_ref=None``).

    Rows use ``product=ROBOT_PRODUCT="hetzner"`` matching what
    :func:`~meho_backplane.operations.ingest.parser.parse_connector_id`
    derives from ``"hetzner-rest-2026.04"``.
    """
    sessionmaker = get_sessionmaker()
    group_ids: dict[str, UUID] = {}
    async with sessionmaker() as session:
        for group in ROBOT_CORE_GROUPS:
            group_row = OperationGroup(
                tenant_id=None,
                product=ROBOT_PRODUCT,
                version=ROBOT_VERSION,
                impl_id=ROBOT_IMPL_ID,
                group_key=group.group_key,
                name=group.name,
                when_to_use=group.when_to_use,
                review_status="enabled",
            )
            session.add(group_row)
            await session.flush()
            group_ids[group.group_key] = group_row.id

        for op in ROBOT_CORE_OPS:
            method, path = op.op_id.split(":", 1)
            descriptor = EndpointDescriptor(
                tenant_id=None,
                product=ROBOT_PRODUCT,
                version=ROBOT_VERSION,
                impl_id=ROBOT_IMPL_ID,
                op_id=op.op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                group_id=group_ids[op.group_key],
                summary=f"Robot core op {op.op_id} (curated read).",
                description=f"Robot core op {op.op_id} (curated read).",
                parameter_schema=_param_schema_for(path),
                response_schema={"type": "object"},
                llm_instructions=op.llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                tags=["spec:hetzner-robot-2026-04/robot-api.yaml"],
            )
            session.add(descriptor)
        await session.commit()


def _param_schema_for(path: str) -> dict[str, object]:
    """Build a minimal ``parameter_schema`` for each ``{var}`` in *path*.

    Mirrors :func:`tests.acceptance._harbor_canary_fixtures._param_schema_for`.
    Robot paths carry path variables like ``{server-ip}`` and ``{id}``.
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


async def _robot_credentials_loader(_target: HetznerRobotTargetLike) -> dict[str, str]:
    """Stub credentials loader — bypasses the not-yet-wired Vault read."""
    return {"username": "robot-canary-user", "password": "robot-canary-pw"}


def _register_robot_routes(mock: respx.MockRouter) -> None:
    """Register the 10 Robot read-op routes on *mock*.

    Robot uses HTTP Basic on every request — no session establish call
    is needed. Each route returns a pre-seeded JSON body. Templated
    paths are registered for specific parameter values the smoke test
    uses (server-ip="1.2.3.1", id=4321).
    """
    mock.get("/query").respond(200, json=ROBOT_CANARY_QUERY)
    mock.get("/server").respond(200, json=ROBOT_CANARY_SERVERS)
    mock.get("/server/1.2.3.1").respond(200, json=ROBOT_CANARY_SERVER_DETAIL)
    mock.get("/ip").respond(200, json=ROBOT_CANARY_IPS)
    mock.get("/subnet").respond(200, json=ROBOT_CANARY_SUBNETS)
    mock.get("/vswitch").respond(200, json=ROBOT_CANARY_VSWITCHES)
    mock.get("/vswitch/4321").respond(200, json=ROBOT_CANARY_VSWITCH_DETAIL)
    mock.get("/failover").respond(200, json=ROBOT_CANARY_FAILOVERS)
    mock.get("/rdns").respond(200, json=ROBOT_CANARY_RDNS)
    mock.get("/key").respond(200, json=ROBOT_CANARY_KEYS)


def _register_robot_sandbox_routes(mock: respx.MockRouter) -> None:
    """Register the 10 Robot sandbox routes — every path returns 200 + empty array.

    Mirrors the Hetzner Robot consumer sandbox behaviour:
    ``https://robot-sandbox.hetzner.com`` returns HTTP 200 with empty JSON
    arrays (``[]``) for every read endpoint when called with any valid Basic
    credential. The sandbox op for ``GET:/query`` returns ``{}`` (an empty
    object, not an array) because the Robot Webservice query endpoint returns
    an object shape. All other read ops return ``[]``.

    Operators use the sandbox before they have a production Robot account;
    the MEHO op surface must tolerate empty-array responses gracefully
    (``status='ok'``, empty ``result``) without crashing.
    """
    mock.get("/query").respond(200, json={})
    mock.get("/server").respond(200, json=[])
    mock.get("/server/1.2.3.1").respond(200, json={})
    mock.get("/ip").respond(200, json=[])
    mock.get("/subnet").respond(200, json=[])
    mock.get("/vswitch").respond(200, json=[])
    mock.get("/vswitch/4321").respond(200, json={})
    mock.get("/failover").respond(200, json=[])
    mock.get("/rdns").respond(200, json=[])
    mock.get("/key").respond(200, json=[])


@pytest.fixture
def robot_acceptance_operator() -> Operator:
    """Frozen :class:`Operator` the Robot dispatch tests act as."""
    return Operator(
        sub="g37-robot-acceptance",
        name="G3.7-T8 Robot Acceptance",
        email=None,
        raw_jwt="<robot-acceptance-raw-jwt>",
        tenant_id=ROBOT_CANARY_OPERATOR_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
    )


@pytest.fixture
async def ingested_robot_canary(
    pg_engine: None,
    robot_acceptance_operator: Operator,
) -> AsyncIterator[IngestedRobotCanary]:
    """Yield a dispatcher-ready Robot setup over a respx-mocked Webservice.

    Setup mirrors :func:`tests.acceptance._harbor_canary_fixtures.ingested_harbor_canary`:

    1. Insert built-in :class:`OperationGroup` + :class:`EndpointDescriptor`
       rows for the 10 curated Robot core ops.
    2. Seed a :class:`Target` with ``product="hetzner"`` and the
       :data:`ROBOT_CANARY_FINGERPRINT` so the resolver binds
       :class:`HetznerRobotConnector`.
    3. Resolve + cache the :class:`HetznerRobotConnector` instance the
       dispatcher will use, patching only its ``_credentials_loader``.
    4. Activate a respx router for :data:`ROBOT_CANARY_BASE_URL` and
       register the Robot REST surface.
    """
    await _insert_robot_descriptors()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=ROBOT_CANARY_OPERATOR_TENANT,
            name=ROBOT_TARGET_NAME,
            aliases=[],
            product="hetzner",
            host=ROBOT_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="hetzner/robot-canary",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=ROBOT_CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._robot_canary_fixtures.ingested_robot_canary",
        )
        session.add(target)
        await session.commit()

    registry = all_connectors_v2()
    connector_cls = registry.get(("hetzner", ROBOT_VERSION, ROBOT_IMPL_ID))
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.hetzner_robot as _robot_pkg

        importlib.reload(_robot_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get(("hetzner", ROBOT_VERSION, ROBOT_IMPL_ID))

    assert connector_cls is HetznerRobotConnector, (
        f"expected HetznerRobotConnector registered for "
        f"(hetzner-robot, {ROBOT_VERSION}, {ROBOT_IMPL_ID}); got {connector_cls!r}"
    )

    instance = cast(HetznerRobotConnector, get_or_create_connector_instance(connector_cls))
    instance._credentials_loader = _robot_credentials_loader  # type: ignore[attr-defined]

    async with respx.mock(
        base_url=ROBOT_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_robot_routes(mock)
        try:
            yield IngestedRobotCanary(
                operator=robot_acceptance_operator,
                connector_id=ROBOT_CONNECTOR_ID,
                target_name=ROBOT_TARGET_NAME,
                base_url=ROBOT_CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()


@pytest.fixture
async def ingested_robot_canary_sandbox(
    pg_engine: None,
    robot_acceptance_operator: Operator,
) -> AsyncIterator[IngestedRobotCanary]:
    """Yield a dispatcher-ready Robot setup where every op returns 200 + empty arrays.

    Models the Hetzner Robot consumer sandbox
    (``https://robot-sandbox.hetzner.com``): all 10 read endpoints respond
    with HTTP 200 + empty JSON arrays (``[]``) or empty objects (``{}``) for
    the query endpoint. Operators who run against the sandbox before they
    have a production Robot account should receive ``status='ok'`` with an
    empty result — not a parse crash.

    Setup is identical to :func:`ingested_robot_canary` except:

    * The seeded :class:`Target` uses :data:`ROBOT_SANDBOX_TARGET_NAME`
      (a separate name to avoid primary-key collision in tests that use
      both fixtures in the same session).
    * The respx router maps every path to ``200 + []`` (or ``200 + {}``)
      via :func:`_register_robot_sandbox_routes`.
    """
    await _insert_robot_descriptors()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = Target(
            tenant_id=ROBOT_CANARY_OPERATOR_TENANT,
            name=ROBOT_SANDBOX_TARGET_NAME,
            aliases=[],
            product="hetzner",
            host=ROBOT_CANARY_BASE_URL.removeprefix("https://"),
            port=443,
            fqdn=None,
            secret_ref="hetzner/robot-sandbox",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint=ROBOT_CANARY_FINGERPRINT,
            notes="seeded by tests.acceptance._robot_canary_fixtures.ingested_robot_canary_sandbox",
        )
        session.add(target)
        await session.commit()

    registry = all_connectors_v2()
    connector_cls = registry.get(("hetzner", ROBOT_VERSION, ROBOT_IMPL_ID))
    if connector_cls is None:
        import importlib

        import meho_backplane.connectors.hetzner_robot as _robot_pkg

        importlib.reload(_robot_pkg)
        registry = all_connectors_v2()
        connector_cls = registry.get(("hetzner", ROBOT_VERSION, ROBOT_IMPL_ID))

    assert connector_cls is HetznerRobotConnector, (
        f"expected HetznerRobotConnector registered for "
        f"(hetzner-robot, {ROBOT_VERSION}, {ROBOT_IMPL_ID}); got {connector_cls!r}"
    )

    instance = cast(HetznerRobotConnector, get_or_create_connector_instance(connector_cls))
    instance._credentials_loader = _robot_credentials_loader  # type: ignore[attr-defined]

    async with respx.mock(
        base_url=ROBOT_CANARY_BASE_URL,
        assert_all_called=False,
        assert_all_mocked=False,
    ) as mock:
        _register_robot_sandbox_routes(mock)
        try:
            yield IngestedRobotCanary(
                operator=robot_acceptance_operator,
                connector_id=ROBOT_CONNECTOR_ID,
                target_name=ROBOT_SANDBOX_TARGET_NAME,
                base_url=ROBOT_CANARY_BASE_URL,
            )
        finally:
            await instance.aclose()
            reset_dispatcher_caches()
