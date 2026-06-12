# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the nsx live credential-read chain (G3.10-T1 #945).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> NsxConnector()           # default loader
      -> dispatch_ingested -> _request_json -> auth_headers
      -> _session_token -> load_session_credentials_from_vault   # the live loader
      -> load_basic_credentials (#941)                           # operator-context Vault read
      -> POST /api/session/create (form body w/ Vault-read creds)
      -> GET <op path>                                           # the actual read op
      -> OperationResult(status="ok")

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane** (``pytest -n auto`` /
``--ignore=tests/integration``) with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against canned creds. The
  default loader is exercised verbatim — *not* an injected stub. This is
  the load-bearing difference from ``test_connectors_nsx_auth.py``
  (which injects ``_stub_loader``): here the connector is constructed by
  the resolver as ``NsxConnector()`` with no ``session_loader``, so the
  default ``load_session_credentials_from_vault`` runs.
* **NSX manager** — respx replays ``POST /api/session/create`` (returns
  ``X-XSRF-TOKEN`` + ``JSESSIONID`` cookie) and the op ``GET`` (returns
  the read payload). No network.

Mirrors :mod:`tests.test_connectors_vmware_rest_credread` for the
vmware-rest connector verbatim (G3.9-T3 #942 precedent) — same lifecycle,
same canary-credential leak assertions.
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
from meho_backplane.connectors.nsx import NsxConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER appear in the result or logs.
# Generated nowhere near a real secret; these strings are the leak canaries.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "svc-nsx-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread-nsx"

#: The connector triple ``nsx-rest-4.2`` decodes to. Deliberately a
#: 4.x label (not the VCF-9-aligned "9.0" class pin) so this test
#: doubles as a regression guard that a standalone NSX-T 4.x target
#: still dispatches through the widened-range NsxConnector (#1530):
#: the test registers this triple against a fresh registry and the
#: target's fingerprint reports ``version=4.2``, so the resolver binds
#: via the ``>=4.0,<10.0`` SpecifierSet, not the class pin.
_PRODUCT = "nsx"
_VERSION = "4.2"
_IMPL_ID = "nsx-rest"
_CONNECTOR_ID = "nsx-rest-4.2"

#: A spec-relative ingested GET op. NSX's hand-rolled connector does not
#: override ``mount_op_path``, so the path is unchanged at dispatch.
_OP_ID = "GET:/api/v1/node"
_OP_PATH = "/api/v1/node"

#: respx base URL the target points at. Port 443 keeps ``_base_url`` from
#: appending ``:port`` so the router matches the connector's client URL.
#: ``.test.invalid`` (RFC 6761) guarantees no real egress.
_NSX_HOST = "nsx-credread.test.invalid"
_NSX_BASE_URL = f"https://{_NSX_HOST}"
_XSRF_TOKEN = "credread-nsx-xsrf-token"

#: The read op's response payload — set-shaped but small enough that the
#: pass-through reducer returns it inline (no handle).
_OP_RESPONSE: dict[str, Any] = {
    "node_version": "4.2.0.0.0.21761695",
    "kernel_version": "5.10.0-nsx",
    "node_uuid": "credread-nsx-node-uuid",
    "hostname": _NSX_HOST,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
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
    """Reset dispatcher caches + connector registry around every test.

    The connector-instance cache must be clear so the resolver builds a
    fresh ``NsxConnector()`` (default loader) for this test rather than
    reusing one a sibling test wired with an injected stub loader.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=NsxConnector,
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
    """Target satisfying both ``NsxTargetLike`` and the resolver shape.

    Carries the resolver attributes (``product`` / ``fingerprint`` /
    ``preferred_impl_id`` / ``id``) plus the nsx-loader attributes
    (``name`` / ``host`` / ``port`` / ``secret_ref`` / ``auth_model``).
    """

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0a1")
        self.name = "nsx-credread"
        self.host = _NSX_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/nsx-credread"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread-nsx",
        name="NSX Cred Read Operator",
        email=None,
        raw_jwt="op.credread.nsx.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_descriptor(session: AsyncSession, embedding: list[float]) -> None:
    """Insert one enabled ingested GET descriptor for the read op."""
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_OP_ID,
        source_kind="ingested",
        method="GET",
        path=_OP_PATH,
        handler_ref=None,
        summary="Read NSX node info",
        description="Return the NSX manager node version + uuid.",
        tags=["readiness"],
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
    """The full dispatch->loader->NSX chain returns status="ok" with no injected loader."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary creds via the
    # real ``load_basic_credentials`` -> ``vault_client_for_operator``
    # path. No session_loader is injected anywhere — the resolver builds
    # ``NsxConnector()`` so the default loader runs.
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_NSX_BASE_URL, assert_all_called=False) as mock:
        session_route = mock.post("/api/session/create").respond(
            200,
            headers={
                "X-XSRF-TOKEN": _XSRF_TOKEN,
                "Set-Cookie": "JSESSIONID=credread-nsx-jsess; Path=/; HttpOnly",
            },
        )
        op_route = mock.get("/api/v1/node").respond(200, json=_OP_RESPONSE)

        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params={},
        )

    # The load-bearing assertion: the chain executed end to end.
    assert result.status == "ok", result.error
    assert isinstance(result.result, dict)
    assert result.result == _OP_RESPONSE

    # The default loader actually read Vault under the operator's identity.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.nsx.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The session establish + the read op both hit the mocked NSX manager.
    assert session_route.called and session_route.call_count == 1
    assert op_route.called and op_route.call_count == 1
    # Form-encoded body carried the Vault-read creds (j_username, j_password).
    sent_body = session_route.calls[0].request.content.decode()
    # The body is form-encoded; specific creds must be present.
    assert "j_username=" in sent_body
    assert "j_password=" in sent_body
    # XSRF token on the read-op GET (cookie travels via the client jar).
    op_xsrf = op_route.calls[0].request.headers.get("x-xsrf-token")
    assert op_xsrf == _XSRF_TOKEN
    # One audit + one broadcast for the dispatched op.
    assert len(captured_events) == 1


@pytest.mark.asyncio
async def test_credread_chain_never_leaks_credential_in_result_or_logs(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential value appears in the OperationResult or any captured log event."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    with capture_logs() as captured:
        async with respx.mock(base_url=_NSX_BASE_URL, assert_all_called=False) as mock:
            mock.post("/api/session/create").respond(
                200,
                headers={
                    "X-XSRF-TOKEN": _XSRF_TOKEN,
                    "Set-Cookie": "JSESSIONID=credread-nsx-jsess; Path=/",
                },
            )
            mock.get("/api/v1/node").respond(200, json=_OP_RESPONSE)

            result = await dispatch(
                operator=operator,
                connector_id=_CONNECTOR_ID,
                op_id=_OP_ID,
                target=target,
                params={},
            )

    assert result.status == "ok", result.error

    # The credential never rides the OperationResult (result, error, or
    # any extras the envelope carries).
    result_blob = repr(result)
    assert _CANARY_PASSWORD not in result_blob
    assert _CANARY_USERNAME not in result_blob

    # The credential never rides any structlog event (loader, connector,
    # dispatcher, audit, broadcast).
    log_blob = repr(captured)
    assert _CANARY_PASSWORD not in log_blob
    assert _CANARY_USERNAME not in log_blob

    # The credential never rides the broadcast event payload either.
    events_blob = repr(captured_events)
    assert _CANARY_PASSWORD not in events_blob
    assert _CANARY_USERNAME not in events_blob
