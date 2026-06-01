# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the argocd live credential-read chain (G3.12-T1 #1390).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> ArgoCdConnector()        # default loader
      -> dispatch_ingested -> _request_json -> auth_headers
      -> _load_credentials -> load_credentials_from_vault        # the live loader
      -> load_basic_credentials (#941), fields=("token",)        # operator-context Vault read
      -> GET <op path> (Authorization: Bearer <Vault-read token>)
      -> OperationResult(status="ok")

ArgoCD differs from Harbor in that the credential is a single bearer token
(no username component) sent as ``Authorization: Bearer <token>`` on every
request. The credential read still goes through the shared operator-context
Vault helper; the token is then placed on the ``Authorization`` header
verbatim.

The two "real" leaves are stubbed at their boundaries so the gate runs in
the secret-free unit lane (``pytest -n auto`` / ``--ignore=tests/integration``)
with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against a canned token. The
  default loader is exercised verbatim — *not* an injected stub.
* **ArgoCD** — respx replays the op ``GET`` (returns the read payload). No
  network.

Mirrors :mod:`tests.test_connectors_harbor_credread` (G3.10-T1 #945
precedent) — same lifecycle, same canary-credential leak assertions.
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
from meho_backplane.connectors.argocd import ArgoCdConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential value — asserted to NEVER appear in the result or logs.
# ---------------------------------------------------------------------------

_CANARY_TOKEN = "argocd-bearer-canary-must-not-leak-credread"

#: The connector triple ``argocd-api-3.x`` decodes to.
_PRODUCT = "argocd"
_VERSION = "3.x"
_IMPL_ID = "argocd-api"
_CONNECTOR_ID = "argocd-api-3.x"

#: A spec-relative ingested GET op against ArgoCD's application list endpoint.
_OP_ID = "GET:/api/v1/applications"
_OP_PATH = "/api/v1/applications"

#: respx base URL the target points at. Port 443 keeps ``_base_url`` from
#: appending ``:port`` so the router matches the connector's client URL.
_ARGOCD_HOST = "argocd-credread.test.invalid"
_ARGOCD_BASE_URL = f"https://{_ARGOCD_HOST}"

#: The read op's response payload — shaped to look like an ArgoCD app list.
_OP_RESPONSE: dict[str, Any] = {
    "metadata": {"resourceVersion": "12345"},
    "items": [{"metadata": {"name": "guestbook"}, "status": {"sync": {"status": "Synced"}}}],
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
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=ArgoCdConnector,
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
    """Target satisfying both ``ArgoCdTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        # ``Connector.supported_version_range`` is ``">=2.0,<4.0"``; the
        # resolver parses target.fingerprint.version as PEP 440, so use a
        # concrete dotted version (not the wildcard ``"3.x"`` triple version
        # that names the connector key but isn't PEP-440 parseable).
        self.fingerprint = type("_FP", (), {"version": "3.3.9"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "argocd-credread"
        self.host = _ARGOCD_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/argocd-credread"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread-argocd",
        name="ArgoCD Cred Read Operator",
        email=None,
        raw_jwt="op.credread.argocd.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a3"),
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
        summary="List ArgoCD applications",
        description="Return the ArgoCD application list.",
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
    """The full dispatch->loader->ArgoCD chain returns status="ok" with no injected loader."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary token via the real
    # ``load_basic_credentials`` -> ``vault_client_for_operator`` path. No
    # credentials_loader is injected — the resolver builds
    # ``ArgoCdConnector()`` so the default loader runs.
    fake = install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
        op_route = mock.get("/api/v1/applications").respond(200, json=_OP_RESPONSE)

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
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.argocd.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The read op hit the mocked ArgoCD instance with a bearer token.
    assert op_route.called and op_route.call_count == 1
    sent_auth = op_route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {_CANARY_TOKEN}"
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

    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})

    target = _CredReadTarget()
    operator = _make_operator()

    with capture_logs() as captured:
        async with respx.mock(base_url=_ARGOCD_BASE_URL, assert_all_called=False) as mock:
            mock.get("/api/v1/applications").respond(200, json=_OP_RESPONSE)

            result = await dispatch(
                operator=operator,
                connector_id=_CONNECTOR_ID,
                op_id=_OP_ID,
                target=target,
                params={},
            )

    assert result.status == "ok", result.error

    # The token never rides the OperationResult (result, error, or extras).
    assert _CANARY_TOKEN not in repr(result)
    # The token never rides any structlog event.
    assert _CANARY_TOKEN not in repr(captured)
    # The token never rides the broadcast event payload either.
    assert _CANARY_TOKEN not in repr(captured_events)
