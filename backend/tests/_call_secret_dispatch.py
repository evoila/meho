# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stand-in vendor for the tests of call secret values, run through the real dispatch.

Every call of :class:`EchoConnector` fails with, or answers with, what the
test's *behave* returns. The op has no masking code of its own, so only the
shared rules can hide a value.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.settings import get_settings

PRODUCT = "echodemo"
VERSION = "1"
TENANT = UUID("00000000-0000-0000-0000-00000000b787")
WRITE_OP = "POST:/api/v1/accounts"
READ_OP = "GET:/api/v1/accounts"


@pytest.fixture(autouse=True)
def required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


def http_error(status: int, body: Any) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://appliance.example.test/api/v1/accounts")
    if isinstance(body, str):
        response = httpx.Response(status, text=body, request=request)
    else:
        response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class EchoConnector(HttpConnector):
    """Every call fails with, or answers with, what :attr:`behave` returns."""

    product = PRODUCT
    accepts_ingested_operations = True
    version = VERSION
    impl_id = "echodemo-rest"
    supported_version_range = ">=1,<2"
    priority = 1

    behave: ClassVar[Callable[[], Any]]

    def _go(self) -> Any:
        outcome = type(self).behave()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _request_json(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self._go()

    async def _post_json(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self._go()

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


class _Fingerprint:
    version = VERSION


class DispatchTarget:
    def __init__(self, product: str = PRODUCT) -> None:
        self.product = product
        self.fingerprint = _Fingerprint()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.tenant_id = TENANT
        self.name = "appliance-a"
        self.host = "appliance.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"
        self.secret_ref: str | None = None


def operator() -> Operator:
    return Operator(
        sub="op-echo",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def seed(
    op_id: str,
    *,
    method: str | None,
    schema: dict[str, Any],
    source_kind: str = "ingested",
    handler_ref: str | None = None,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product=PRODUCT,
                version=VERSION,
                impl_id=EchoConnector.impl_id,
                op_id=op_id,
                source_kind=source_kind,
                method=method,
                path="/api/v1/accounts" if method else None,
                handler_ref=handler_ref,
                summary="Made-up op.",
                description="Made-up op with no masking code of its own.",
                tags=["accounts"],
                parameter_schema=schema,
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=[0.1] * 384,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def run_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    op_id: str,
    behave: Callable[[], Any],
    params: dict[str, Any],
    schema: dict[str, Any] | None = None,
    source_kind: str = "ingested",
    handler_ref: str | None = None,
) -> tuple[Any, AuditLog]:
    """Run *op_id* once through the real dispatch; return the answer and its audit row."""
    monkeypatch.setattr(EchoConnector, "behave", staticmethod(behave), raising=False)
    register_connector_v2(
        product=PRODUCT, version=VERSION, impl_id=EchoConnector.impl_id, cls=EchoConnector
    )
    if source_kind == "ingested":
        method: str | None = "GET" if op_id.startswith("GET") else "POST"
    else:
        method = None
    await seed(
        op_id,
        method=method,
        schema=schema or {"type": "object", "additionalProperties": True},
        source_kind=source_kind,
        handler_ref=handler_ref,
    )
    result = await dispatch(
        operator=operator(),
        connector_id=f"{EchoConnector.impl_id}-{VERSION}",
        op_id=op_id,
        target=DispatchTarget(),
        params=params,
    )
    assert get_or_create_connector_instance(EchoConnector) is not None
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    assert len(rows) == 1
    return result, rows[0]
