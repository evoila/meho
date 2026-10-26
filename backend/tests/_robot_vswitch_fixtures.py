# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared setup for the Hetzner Robot vSwitch write tests (#3973).

Used by ``test_connectors_hetzner_robot_form_writes.py`` (what goes on the
wire) and ``test_connectors_hetzner_robot_vswitch_governance.py`` (who may
run it and when). Both ingest the shipped minimal spec through the real
ingest entry point, so the connector's safety floor applies exactly as on a
live backplane, then switch on the ops under test and seed one Robot target.
The Robot Webservice itself is mocked with respx in each test.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib.resources import files
from unittest.mock import AsyncMock
from uuid import UUID

from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.hetzner_robot import (
    ROBOT_IMPL_ID,
    ROBOT_PRODUCT,
    ROBOT_VERSION,
    HetznerRobotConnector,
    HetznerRobotTargetLike,
)
from meho_backplane.connectors.hetzner_robot.ingest_safety import register_safety_floor
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Target
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations

SPEC_RESOURCE = "hetzner_robot_minimal.yaml"
TENANT = UUID("00000000-0000-0000-0000-000000003973")
ROBOT_HOST = "robot-ws.test.invalid"
BASE_URL = f"https://{ROBOT_HOST}"
TARGET_NAME = "robot-vswitch"
VSWITCH_ID = "4321"

ADD_OP = "POST:/vswitch/{vswitch-id}/server"
REMOVE_OP = "DELETE:/vswitch/{vswitch-id}/server"
RENAME_OP = "POST:/vswitch/{vswitch-id}"
CANCEL_OP = "DELETE:/vswitch/{vswitch-id}"
FIREWALL_SET_OP = "POST:/firewall/{server-ip}"


def make_embedding_service() -> AsyncMock:
    """A stub embedding service so ingest does not load a model."""
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def register_robot_connector() -> None:
    """Register the Robot connector (and its floor) after other tests cleared them."""
    clear_registry()
    register_connector_v2(
        product=HetznerRobotConnector.product,
        version=HetznerRobotConnector.version,
        impl_id=HetznerRobotConnector.impl_id,
        cls=HetznerRobotConnector,
    )
    register_connector_v2(product="hetzner", version="", impl_id="", cls=HetznerRobotConnector)
    register_safety_floor()


async def ingest_shipped_spec(embedding_service: AsyncMock) -> None:
    """Ingest the shipped Robot spec through the real ingest entry point."""
    content = files("meho_backplane.operations.ingest.specs").joinpath(SPEC_RESOURCE)
    operations = parse_openapi(
        f"https://specs.example.test/{SPEC_RESOURCE}",
        spec_source=f"spec:{SPEC_RESOURCE}",
        content=content.read_text(encoding="utf-8"),
    )
    await register_ingested_operations(
        product=ROBOT_PRODUCT,
        version=ROBOT_VERSION,
        impl_id=ROBOT_IMPL_ID,
        spec_source=SPEC_RESOURCE,
        operations=operations,
        embedding_service=embedding_service,
    )


async def load_rows(op_ids: Iterable[str]) -> dict[str, EndpointDescriptor]:
    """Return the ingested Robot rows for *op_ids*, keyed by op_id."""
    stmt = select(EndpointDescriptor).where(
        EndpointDescriptor.product == ROBOT_PRODUCT,
        EndpointDescriptor.version == ROBOT_VERSION,
        EndpointDescriptor.impl_id == ROBOT_IMPL_ID,
        EndpointDescriptor.op_id.in_(list(op_ids)),
    )
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return {row.op_id: row for row in rows}


async def enable_ops(*op_ids: str) -> None:
    """Switch on *op_ids* (the ingest default is off)."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(
                EndpointDescriptor.product == ROBOT_PRODUCT,
                EndpointDescriptor.version == ROBOT_VERSION,
                EndpointDescriptor.impl_id == ROBOT_IMPL_ID,
                EndpointDescriptor.op_id.in_(op_ids),
            )
            .values(is_enabled=True)
        )
        await session.commit()


async def seed_target() -> Target:
    """Seed one Robot target and return it."""
    target = Target(
        tenant_id=TENANT,
        name=TARGET_NAME,
        aliases=[],
        product="hetzner",
        host=ROBOT_HOST,
        port=443,
        fqdn=None,
        secret_ref="hetzner/robot-vswitch",
        auth_model="shared_service_account",
        vpn_required=False,
        extras={},
        fingerprint={"vendor": "hetzner", "product": "robot-webservice", "reachable": True},
        notes="seeded by the Robot vSwitch write tests",
    )
    async with get_sessionmaker()() as session:
        session.add(target)
        await session.commit()
        await session.refresh(target)
    return target


async def _stub_loader(_target: HetznerRobotTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "webservice-user", "password": "stub-password"}


def install_connector() -> HetznerRobotConnector:
    """Put a stub-credential connector in the dispatcher's instance cache."""
    reset_dispatcher_caches()
    instance = HetznerRobotConnector(credentials_loader=_stub_loader)
    _CONNECTOR_INSTANCE_CACHE[HetznerRobotConnector] = instance
    return instance


def make_operator(
    sub: str = "robot-requester",
    *,
    principal_kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    """An operator-role principal of the given kind."""
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<robot-vswitch-raw-jwt>",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


def membership_params(*servers: object) -> dict[str, object]:
    """The params for a membership add or remove on the test vSwitch."""
    return {"vswitch-id": VSWITCH_ID, "body": {"server": list(servers)}}
