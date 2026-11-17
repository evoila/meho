# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared set-up for the tests of an ingested operation whose class is only stored.

A small API description with ``GET /clients`` and ``POST /clients`` is
ingested with a connector floor that stores a class on the operation row.
The op id of each operation says only ``read`` or ``write``, so every
place that reads the class from the op id alone gets it wrong.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar
from unittest.mock import AsyncMock

from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import get_connector_v2, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations
from meho_backplane.operations.ingest.safety_floors import _FLOORS, register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

PRODUCT = "storedclass"
IMPL_ID = "storedclass-rest"
VERSION = "1.0"
CONNECTOR_ID = f"{IMPL_ID}-{VERSION}"
GET_CLIENTS = "GET:/clients"
POST_CLIENTS = "POST:/clients"
MADE_UP_VALUE = "made-up-" + "stored-class-value"

SPEC = """
openapi: 3.0.3
info: {title: stored class fixture, version: '1.0'}
paths:
  /clients:
    get:
      operationId: clients.list
      parameters:
        - {name: name, in: query, schema: {type: string}}
        - {name: q, in: query, schema: {type: string}}
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
                  properties: {name: {type: string}}
    post:
      operationId: clients.create
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties: {name: {type: string}}
      responses:
        '200': {description: ok}
"""


class Clients(Connector):
    """The connector of the fixture: it answers every request with ``answer``."""

    product = PRODUCT
    version = VERSION
    impl_id = IMPL_ID
    accepts_ingested_operations = True
    calls: ClassVar[list[str]] = []
    answer: ClassVar[Any] = [{"name": "a"}]
    failure: ClassVar[Exception | None] = None

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise AssertionError("an ingested op runs through _request_json")

    async def _request_json(self, target: Any, method: str, path: str, **_: Any) -> Any:
        type(self).calls.append(f"{method}:{path}")
        if type(self).failure is not None:
            raise type(self).failure
        return type(self).answer

    async def _post_json(self, target: Any, path: str, **kwargs: Any) -> Any:
        return await self._request_json(target, kwargs.get("verb", "POST"), path)


class Target:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.name = "stored-class-target"
        self.product = PRODUCT
        self.fingerprint = type("Fingerprint", (), {"version": VERSION})()
        self.preferred_impl_id: str | None = None


def operator(
    kind: PrincipalKind = PrincipalKind.USER, tenant_id: uuid.UUID | None = None
) -> Operator:
    return Operator(
        sub=f"sub-{uuid.uuid4()}",
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id or uuid.uuid4(),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


def _floor(classes: dict[str, str]) -> Any:
    def floor(version: str, proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
        stored = classes.get(proto.op_id)
        return proto if stored is None else proto.model_copy(update={"op_class": stored})

    return floor


async def ingest(
    service: AsyncMock, classes: dict[str, str], *, approval: bool = False
) -> dict[str, EndpointDescriptor]:
    """Ingest the fixture with a floor that stores *classes*, and enable both operations.

    *approval* turns approval on for the two rows, as a reviewer may.
    """
    if get_connector_v2(PRODUCT, VERSION, IMPL_ID) is None:
        register_connector_v2(product=PRODUCT, version=VERSION, impl_id=IMPL_ID, cls=Clients)
    _FLOORS.clear()
    register_ingest_safety_floor(product=PRODUCT, impl_id=IMPL_ID, floor=_floor(classes))
    await register_ingested_operations(
        product=PRODUCT,
        version=VERSION,
        impl_id=IMPL_ID,
        spec_source="stored-class.yaml",
        operations=parse_openapi("file:///stored-class.yaml", content=SPEC),
        embedding_service=service,
    )
    _FLOORS.clear()
    async with get_sessionmaker()() as session, session.begin():
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == PRODUCT)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.is_enabled = True
            if approval:
                row.requires_approval = True
                if row.safety_level == "safe":
                    row.safety_level = "caution"
        return {row.op_id: row for row in rows}


async def call(
    who: Operator,
    op_id: str = GET_CLIENTS,
    params: dict[str, Any] | None = None,
    *,
    approved: bool = False,
) -> Any:
    return await dispatch(
        operator=who,
        connector_id=CONNECTOR_ID,
        op_id=op_id,
        target=Target(),
        params=params or {},
        _approved=approved,
    )
