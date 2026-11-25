# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The start runs the check of what a connector declares for its target form.

Every test here goes through the real start path
(:func:`~meho_backplane.operations.typed_register.operation_catalog_write`)
with the test-only registrar snapshot turned off, unless it says otherwise.

* A fact of the code stops the start, with a message that names the class: a
  broken declaration, a ``test_operation`` that no registrar of the connector
  writes, a ``test_operation`` whose code gives it a risk level other than
  ``safe``, and a ``test_operation`` that needs input.
* A value that lives only in the database never stops the start: a stored
  risk level on the row, the row's turned-off mark, or an override. MEHO logs
  one warning line, starts, and the target form route answers
  ``test_operation: null`` for the class.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from structlog.testing import capture_logs

from meho_backplane.api.v1.connectors_target_form import router
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import Connector
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
from meho_backplane.connectors.target_form import (
    ConnectorDeclarationError,
    CredentialField,
    CredentialWay,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import typed_register
from meho_backplane.operations.connector_declaration_check import (
    assert_connector_declarations_valid,
)
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_PRODUCT, _VERSION, _IMPL_ID = "formfake", "1.x", "formfake-rest"
_TENANT = uuid.uuid4()

#: What :func:`formfake_registrar` writes: op id -> (risk level, parameter schema).
_OPS: dict[str, tuple[str, dict[str, Any]]] = {}


async def formfake_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Handler of every fake op."""
    return {}


async def formfake_registrar(*, embedding_service: Any = None) -> None:
    """Register the ops in :data:`_OPS` for the fake connector."""
    for op_id, (safety_level, schema) in _OPS.items():
        await register_typed_operation(
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL_ID,
            op_id=op_id,
            handler=formfake_handler,
            summary=f"Fake {op_id}",
            description=f"Fake {op_id}.",
            parameter_schema=schema,
            when_to_use=None,
            safety_level=safety_level,  # type: ignore[arg-type]
            requires_approval=safety_level == "destructive",
            embedding_service=embedding_service,
        )


class _Base(Connector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


def _make(**attrs: Any) -> type[Connector]:
    return type("FakeConnector", (_Base,), attrs)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Required settings and a real registrar pass (no snapshot replay)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", "false")
    _OPS.clear()
    get_settings.cache_clear()
    yield
    _OPS.clear()
    get_settings.cache_clear()


def _embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _start() -> None:
    """Run the start's catalog write, the same path the app start uses."""
    async with typed_register.operation_catalog_write(embedding_service=_embedding_service()):
        pass


def _only_fake(monkeypatch: pytest.MonkeyPatch, cls: type[Connector]) -> None:
    """Make the fake connector the only registered class and registrar."""
    monkeypatch.setattr(connector_registry, "_REGISTRY_V2", {(_PRODUCT, _VERSION, _IMPL_ID): cls})
    monkeypatch.setattr(typed_register, "_TYPED_OP_REGISTRARS", [formfake_registrar])


def _route_test_operation(cls: type[Connector]) -> Any:
    """``test_operation`` of *cls* in the answer of the target form route."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_jwt_and_bind] = lambda: Operator(
        sub="op", raw_jwt="test-jwt", tenant_id=_TENANT, tenant_role=TenantRole.OPERATOR
    )
    resp = TestClient(app).get("/api/v1/connectors/target-form", params={"product": _PRODUCT})
    assert resp.status_code == 200, resp.text
    (answer,) = resp.json()["classes"]
    return answer["test_operation"]


# ---------------------------------------------------------------------------
# A fact of the code stops the start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_start_stops_for_a_broken_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    twice = CredentialWay(
        key="basic", label="Basic", fields=(CredentialField(name="password", label="Password"),)
    )
    _only_fake(monkeypatch, _make(credential_ways=(twice, twice)))
    with pytest.raises(ConnectorDeclarationError, match=r"FakeConnector.*'basic' is twice"):
        await _start()


@pytest.mark.asyncio
async def test_the_start_stops_for_a_test_read_no_registrar_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OPS["formfake.ping"] = ("safe", {"type": "object"})
    _only_fake(monkeypatch, _make(test_operation="formfake.missing"))
    with pytest.raises(ConnectorDeclarationError, match=r"FakeConnector.*no registrar"):
        await _start()


@pytest.mark.asyncio
async def test_a_test_read_of_another_connector_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            EndpointDescriptor(
                tenant_id=None,
                product="other",
                version="1.x",
                impl_id="other-rest",
                op_id="other.ping",
                source_kind="typed",
                parameter_schema={"type": "object"},
                safety_level="safe",
                is_enabled=True,
            )
        )
    _only_fake(monkeypatch, _make(test_operation="other.ping"))
    with pytest.raises(ConnectorDeclarationError, match=r"FakeConnector.*no registrar"):
        await _start()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["caution", "dangerous", "destructive"])
async def test_the_start_stops_when_the_code_gives_the_test_read_another_risk_level(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    _OPS["formfake.change"] = (level, {"type": "object"})
    _only_fake(monkeypatch, _make(test_operation="formfake.change"))
    with pytest.raises(
        ConnectorDeclarationError, match=rf"FakeConnector.*'{level}' in its code.*must be 'safe'"
    ):
        await _start()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        {
            "type": "object",
            "properties": {"scope": {"type": "string"}, "all": {"const": True}},
            "oneOf": [{"required": ["scope"]}, {"required": ["all"]}],
        },
    ],
    ids=["required", "one-of-two"],
)
async def test_the_start_stops_for_a_test_read_that_needs_input(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]
) -> None:
    _OPS["formfake.get"] = ("safe", schema)
    _only_fake(monkeypatch, _make(test_operation="formfake.get"))
    with pytest.raises(ConnectorDeclarationError, match=r"FakeConnector.*needs input"):
        await _start()


@pytest.mark.asyncio
async def test_the_shipped_connectors_pass_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every real registrar runs; Harbor, Linux and Kubernetes declare a good test read."""
    await _start()


@pytest.mark.asyncio
async def test_a_pod_list_test_read_stops_the_start_and_names_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``k8s.pod.list`` needs a namespace or a request for all namespaces."""
    monkeypatch.setattr(KubernetesConnector, "test_operation", "k8s.pod.list")
    with pytest.raises(
        ConnectorDeclarationError, match=r"KubernetesConnector.*'k8s.pod.list' needs input"
    ):
        await _start()


# ---------------------------------------------------------------------------
# A value in the database never stops the start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_safe_enabled_test_read_is_in_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    _OPS["formfake.ping"] = ("safe", {"type": "object"})
    cls = _make(test_operation="formfake.ping")
    _only_fake(monkeypatch, cls)
    with capture_logs() as logs:
        await _start()
    assert not [e for e in logs if e["event"] == "connector_test_operation_not_ready"]
    assert _route_test_operation(cls) == "formfake.ping"


async def _start_twice_with(monkeypatch: pytest.MonkeyPatch, change: Any) -> tuple[Any, list[Any]]:
    """Start once, *change* the database, start again; return the class and the warnings."""
    _OPS["formfake.ping"] = ("safe", {"type": "object"})
    cls = _make(test_operation="formfake.ping")
    _only_fake(monkeypatch, cls)
    await _start()
    async with get_sessionmaker()() as session, session.begin():
        await change(session)
    with capture_logs() as logs:
        await _start()
    warnings = [e for e in logs if e["event"] == "connector_test_operation_not_ready"]
    return cls, warnings


def _row_update(**values: Any) -> Any:
    async def change(session: Any) -> None:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == "formfake.ping")
            .values(**values)
        )

    return change


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["caution", "dangerous", "destructive"])
async def test_a_stored_risk_level_warns_starts_and_hides_the_test_read(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    change = _row_update(safety_level=level, requires_approval=level == "destructive")
    cls, warnings = await _start_twice_with(monkeypatch, change)
    assert [(w["connector"], w["op_id"], w["stored_value"]) for w in warnings] == [
        ("FakeConnector", "formfake.ping", f"stored risk level {level!r}")
    ]
    assert warnings[0]["log_level"] == "warning"
    assert _route_test_operation(cls) is None


@pytest.mark.asyncio
async def test_a_turned_off_row_warns_starts_and_hides_the_test_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cls, warnings = await _start_twice_with(monkeypatch, _row_update(is_enabled=False))
    assert [w["stored_value"] for w in warnings] == ["the row is turned off"]
    assert _route_test_operation(cls) is None


@pytest.mark.asyncio
async def test_an_override_warns_starts_and_hides_the_test_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def add_override(session: Any) -> None:
        session.add(
            OperationPolicyOverride(
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id="formfake.ping",
                safety_level="caution",
                set_by_sub="admin",
            )
        )

    cls, warnings = await _start_twice_with(monkeypatch, add_override)
    assert [w["stored_value"] for w in warnings] == [
        "an override (install) sets risk level 'caution'"
    ]
    assert _route_test_operation(cls) is None


# ---------------------------------------------------------------------------
# The test-only registrar snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_code_level_is_allowed_only_for_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot replay runs no registrar, so no code level is known."""
    _only_fake(monkeypatch, _make(test_operation="formfake.missing"))
    async with get_sessionmaker()() as session:
        await assert_connector_declarations_valid(session, code_levels={}, allow_missing_rows=True)
        with pytest.raises(ConnectorDeclarationError, match="no registrar"):
            await assert_connector_declarations_valid(session, code_levels={})
