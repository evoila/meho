# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The flight recorder stores Keycloak answers with every secret field masked.

Two layers are pinned here:

* the connector hook ``KeycloakConnector.trace_response_body`` alone, with
  no event loop and no HTTP;
* one real ``keycloak.client.get`` dispatch with the recorder on, against a
  respx-mocked Admin REST API: the stored trace and the agent copy of it
  hold the client's fields but not its secret.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
import meho_backplane.flight_recorder.agent_read as agent_read_mod
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.connector import _TRACE_BODY_WITHHELD
from meho_backplane.connectors.keycloak.redaction import REDACTED
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.agent_read import AGENT_TRACE_HANDLE_EXTRA_KEY
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.operations.result_query import read_result_window
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "keycloak-trace-mask"
_KC_HOST = "keycloak-trace-mask.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_MANAGED_REALM = "example-realm"
_CLIENT_UUID = "33333333-3333-3333-3333-333333333333"
_MADE_UP_SECRET = "made-up-client-secret-0123456789abcdef"
_MADE_UP_SMTP_PASSWORD = "made-up-smtp-password-fedcba9876543210"

_CLIENT_ANSWER: dict[str, Any] = {
    "id": _CLIENT_UUID,
    "clientId": "example-app",
    "enabled": True,
    "publicClient": False,
    "secret": _MADE_UP_SECRET,
    "redirectUris": ["https://app.example.test/callback"],
    "protocolMappers": [
        {"name": "tenant_id", "protocol": "openid-connect", "config": {"claim.name": "tenant_id"}}
    ],
}


def _stub_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="admin-cli", client_secret="stub-secret")

    return _load()


# ---------------------------------------------------------------------------
# The hook alone
# ---------------------------------------------------------------------------


def test_the_trace_hook_masks_every_secret_field() -> None:
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    answer = {
        "client": _CLIENT_ANSWER,
        "smtpServer": {"host": "smtp.example.test", "password": _MADE_UP_SMTP_PASSWORD},
    }

    recorded = connector.trace_response_body(httpx.Response(200, json=answer))

    text = json.dumps(recorded)
    assert _MADE_UP_SECRET not in text
    assert _MADE_UP_SMTP_PASSWORD not in text
    assert recorded["client"]["secret"] == REDACTED
    assert recorded["smtpServer"]["password"] == REDACTED
    assert recorded["client"]["clientId"] == "example-app"
    assert recorded["client"]["redirectUris"] == ["https://app.example.test/callback"]


def test_the_trace_hook_masks_a_list_answer() -> None:
    connector = KeycloakConnector(credentials_loader=_stub_loader)

    recorded = connector.trace_response_body(httpx.Response(200, json=[_CLIENT_ANSWER]))

    assert _MADE_UP_SECRET not in json.dumps(recorded)
    assert recorded[0]["secret"] == REDACTED


def test_the_trace_hook_withholds_a_body_that_is_not_json() -> None:
    connector = KeycloakConnector(credentials_loader=_stub_loader)

    recorded = connector.trace_response_body(httpx.Response(502, text="<html>gateway</html>"))

    assert recorded == _TRACE_BODY_WITHHELD


def test_the_trace_hook_records_an_empty_body_as_it_is() -> None:
    connector = KeycloakConnector(credentials_loader=_stub_loader)

    assert connector.trace_response_body(httpx.Response(204)) is None


def test_the_trace_hook_withholds_a_body_the_mask_cannot_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meho_backplane.connectors.keycloak.connector as connector_mod

    def _boom(_value: Any) -> Any:
        raise RuntimeError("mask failed")

    monkeypatch.setattr(connector_mod, "redact_secret_fields", _boom)
    connector = KeycloakConnector(credentials_loader=_stub_loader)

    recorded = connector.trace_response_body(httpx.Response(200, json=_CLIENT_ANSWER))

    assert recorded == _TRACE_BODY_WITHHELD
    assert _MADE_UP_SECRET not in json.dumps(recorded)


# ---------------------------------------------------------------------------
# One recorded keycloak.client.get dispatch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


class _FakeRedis:
    """In-memory async Valkey stand-in for the result handle store."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


@pytest.fixture
def shared_store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    """One store wired into both the agent copy mint and the result_query read."""
    store = ResultHandleStore(_FakeRedis())  # type: ignore[arg-type]
    monkeypatch.setattr(agent_read_mod, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    return store


async def _seed_tenant_with_recorder_on() -> UUID:
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            Tenant(
                id=tenant_id,
                slug=f"kc-trace-{tenant_id.hex[:8]}",
                name="Keycloak trace mask",
                flight_recorder_enabled=True,
            )
        )
        await session.commit()
    return tenant_id


async def _seed_keycloak_target(tenant_id: UUID) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=tenant_id,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="example/keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _MANAGED_REALM},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_connectors_keycloak_trace_mask",
            )
        )
        await session.commit()


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="keycloak-trace-mask-operator",
        name="Keycloak trace mask operator",
        email=None,
        raw_jwt="<keycloak-trace-mask-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture
async def recorded_keycloak() -> AsyncIterator[Operator]:
    """Register the ops, seed a tenant with the recorder on, and a target in it."""
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    tenant_id = await _seed_tenant_with_recorder_on()
    await _seed_keycloak_target(tenant_id)
    connector = KeycloakConnector(credentials_loader=_stub_loader)
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector  # type: ignore[assignment]
    yield _operator(tenant_id)
    await connector.aclose()


async def _stored_spans(tenant_id: UUID) -> tuple[DispatchTrace, list[DispatchTraceSpan]]:
    async with get_sessionmaker()() as session:
        header = (
            await session.execute(select(DispatchTrace).where(DispatchTrace.tenant_id == tenant_id))
        ).scalar_one()
        spans = list(
            (
                await session.execute(
                    select(DispatchTraceSpan)
                    .where(DispatchTraceSpan.trace_id == header.id)
                    .order_by(DispatchTraceSpan.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        return header, spans


@pytest.mark.asyncio
async def test_client_get_trace_and_agent_copy_hold_no_client_secret(
    recorded_keycloak: Operator,
    shared_store: ResultHandleStore,
) -> None:
    operator = recorded_keycloak
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"/admin/realms/{_MANAGED_REALM}/clients/{_CLIENT_UUID}").respond(
            200, json=_CLIENT_ANSWER
        )
        result = await call_operation(
            operator,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.client.get",
                "target": {"name": _TARGET_NAME},
                "params": {"id": _CLIENT_UUID},
            },
        )
    assert result["status"] == "ok", f"client.get failed: {result.get('error')}"
    assert _MADE_UP_SECRET not in json.dumps(result)

    # The stored trace: the vendor answer is recorded, with the secret masked.
    header, spans = await _stored_spans(operator.tenant_id)
    assert header.redaction_uncertain is False
    vendor_calls = [span for span in spans if span.span_kind == "vendor_call"]
    assert len(vendor_calls) == 1
    body = vendor_calls[0].attributes["response_body"]
    assert vendor_calls[0].attributes["body_recorded"] is True
    assert body["clientId"] == "example-app"
    assert body["redirectUris"] == ["https://app.example.test/callback"]
    assert body["secret"] == REDACTED
    assert _MADE_UP_SECRET not in json.dumps([span.attributes for span in spans])
    assert _MADE_UP_SECRET not in json.dumps([span.name for span in spans])

    # The agent copy: the handle pages the same spans back, still masked.
    handle_json = result["extras"][AGENT_TRACE_HANDLE_EXTRA_KEY]
    window = await read_result_window(operator, UUID(handle_json["handle_id"]), offset=0, limit=50)
    assert window["total_rows"] >= 1
    assert _MADE_UP_SECRET not in json.dumps(window)
    assert _MADE_UP_SECRET not in json.dumps(handle_json)
