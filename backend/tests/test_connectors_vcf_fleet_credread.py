# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the VCF Fleet live credential-read chain (G3.10-T2 #946).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> VcfFleetConnector()   # default loader
      -> dispatch_ingested -> auth_headers
      -> load_credentials_from_vault           # the live shared loader
      -> load_basic_credentials (G3.9-T2)      # operator-context Vault read
      -> GET /lcm/lcops/api/v2/datacenters     # the actual read op (Basic auth)
      -> OperationResult(status="ok")

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane**. Same layout as the vROps credread test
— Fleet uses HTTP Basic on every request (stateless, like vROps), so
there is no session-establish step to mock; the single op carries the
Vault-read credentials in ``Authorization: Basic <b64>``.

Fleet's auth uses the literal ``admin@local`` username (LCM-local user
store, no SSO federation — verified against the consumer wrapper
``scripts/vcf-fleet.sh``). The connector sends the username **verbatim**
from the Vault-loaded credentials; this test stores ``admin@local`` as
the canary username so the assertion that the local user store form
round-trips through the loader unchanged is captured here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_fleet import VcfFleetConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER appear in the result or logs.
# Fleet's local user store uses ``admin@local`` verbatim — the @local is
# part of the username, not a realm decoration. Capture that shape here.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "admin@local"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread-fleet"

#: The connector triple ``fleet-rest-9.0`` decodes to.
_PRODUCT_DESCRIPTOR = "fleet"
_PRODUCT_TARGET = "vcf-fleet"
_VERSION = "9.0"
_IMPL_ID = "fleet-rest"
_CONNECTOR_ID = "fleet-rest-9.0"

#: A spec-relative ingested GET op. ``/lcm/lcops/api/v2/datacenters`` is
#: the wrapper-verified reachability surface that proves both transport
#: and HTTP Basic auth on Fleet 9.0 (the diagnostic endpoints all return
#: HTTP 500 in 9.0 builds — see ``vcf_fleet/connector.py`` module docstring).
_OP_ID = "GET:/lcm/lcops/api/v2/datacenters"
_OP_PATH = "/lcm/lcops/api/v2/datacenters"

#: respx base URL.
_FLEET_HOST = "fleet-credread.test.invalid"
_FLEET_BASE_URL = f"https://{_FLEET_HOST}"

#: The read op's response payload — Fleet's datacenters endpoint returns
#: a JSON list. Small enough that the pass-through reducer returns it
#: inline (no handle).
_OP_RESPONSE: list[dict[str, Any]] = [
    {"id": "dc-01", "name": "demo-datacenter"},
]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT_TARGET,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=VcfFleetConnector,
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub for the seeded descriptor row."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Record broadcast events so the audit/broadcast leg is asserted."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _CredReadTarget:
    """Target satisfying both ``VcfFleetTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = _PRODUCT_TARGET
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0a3")
        self.name = "fleet-credread"
        self.host = _FLEET_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/fleet-credread"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread-fleet",
        name="Cred Read Fleet Operator",
        email=None,
        raw_jwt="op.credread.fleet.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a3"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_descriptor(session: AsyncSession, embedding: list[float]) -> None:
    """Insert one enabled ingested GET descriptor for the read op."""
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=_PRODUCT_DESCRIPTOR,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_OP_ID,
        source_kind="ingested",
        method="GET",
        path=_OP_PATH,
        handler_ref=None,
        summary="List Fleet datacenters",
        description="Returns the list of LCM-managed datacenters.",
        tags=["inventory"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()


@pytest.mark.asyncio
async def test_dispatch_executes_full_credread_chain_returns_ok(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full dispatch->loader->Fleet chain returns status="ok" with no injected loader."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
        op_route = mock.get(_OP_PATH).respond(200, json=_OP_RESPONSE)

        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params={},
        )

    # The load-bearing assertion: the chain executed end to end.
    assert result.status == "ok", result.error
    assert result.result == _OP_RESPONSE

    # The default loader actually read Vault under the operator's identity.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.fleet.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The op route was hit, and the Basic-auth header carried the read creds.
    assert op_route.called and op_route.call_count == 1
    sent_auth = op_route.calls[0].request.headers.get("authorization")
    assert sent_auth is not None and sent_auth.startswith("Basic ")
    # Decode and assert the username is sent verbatim — Fleet's
    # local-user store form ``admin@local`` is NOT a realm decoration;
    # the connector must not strip or rewrite it.
    import base64

    decoded = base64.b64decode(sent_auth[len("Basic ") :]).decode()
    username, _, password = decoded.partition(":")
    assert username == _CANARY_USERNAME
    assert password == _CANARY_PASSWORD
    # One audit + one broadcast for the dispatched op.
    assert len(captured_events) == 1


@pytest.mark.asyncio
async def test_credread_chain_never_leaks_credential_in_result_or_logs(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential value appears in the OperationResult or any captured log event.

    Specifically asserts the password is not leaked — the username
    ``admin@local`` IS the canary here too, but Fleet's auth contract
    requires that exact literal in the Basic header, so we only assert
    the *password* is absent. The general no-secret-in-logs discipline
    is enforced by the shared
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`
    helper (G3.9-T2) — this test confirms the discipline survives the
    full dispatch chain end to end.
    """
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    with capture_logs() as captured:
        async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
            mock.get(_OP_PATH).respond(200, json=_OP_RESPONSE)

            result = await dispatch(
                operator=operator,
                connector_id=_CONNECTOR_ID,
                op_id=_OP_ID,
                target=target,
                params={},
            )

    assert result.status == "ok", result.error

    # The password never rides the OperationResult.
    result_blob = repr(result)
    assert _CANARY_PASSWORD not in result_blob

    # The password never rides any structlog event.
    log_blob = repr(captured)
    assert _CANARY_PASSWORD not in log_blob

    # The password never rides the broadcast event payload either.
    events_blob = repr(captured_events)
    assert _CANARY_PASSWORD not in events_blob
