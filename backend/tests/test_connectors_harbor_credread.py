# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the harbor live credential-read chain (G3.10-T1 #945).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> HarborConnector()        # default loader
      -> dispatch_ingested -> _request_json -> auth_headers
      -> _load_credentials -> load_credentials_from_vault        # the live loader
      -> load_basic_credentials (#941)                           # operator-context Vault read
      -> GET <op path> (HTTP Basic auth w/ Vault-read creds)
      -> OperationResult(status="ok")

Harbor differs from nsx/vmware in that there is no session establish
call — HTTP Basic auth rides every request. The credential read still
goes through the shared operator-context Vault helper; the cred is
then base64-encoded into the ``Authorization`` header on every API call.

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane** (``pytest -n auto`` /
``--ignore=tests/integration``) with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against canned creds. The
  default loader is exercised verbatim — *not* an injected stub.
* **Harbor** — respx replays the op ``GET`` (returns the read payload).
  No network.

Mirrors :mod:`tests.test_connectors_vmware_rest_credread` (G3.9-T3 #942
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
from meho_backplane.connectors.harbor import HarborConnector
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

_CANARY_USERNAME = "svc-harbor-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread-harbor"

#: The connector triple ``harbor-rest-2.x`` decodes to.
_PRODUCT = "harbor"
_VERSION = "2.x"
_IMPL_ID = "harbor-rest"
_CONNECTOR_ID = "harbor-rest-2.x"

#: A spec-relative ingested GET op against Harbor's systeminfo endpoint.
_OP_ID = "GET:/api/v2.0/systeminfo"
_OP_PATH = "/api/v2.0/systeminfo"

#: respx base URL the target points at. Port 443 keeps ``_base_url`` from
#: appending ``:port`` so the router matches the connector's client URL.
_HARBOR_HOST = "harbor-credread.test.invalid"
_HARBOR_BASE_URL = f"https://{_HARBOR_HOST}"

#: The read op's response payload — shaped to look like Harbor systeminfo.
_OP_RESPONSE: dict[str, Any] = {
    "harbor_version": "v2.11.0-credread",
    "auth_mode": "db_auth",
    "registry_url": "harbor-credread.test.invalid",
    "external_url": "https://harbor-credread.test.invalid",
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
    fresh ``HarborConnector()`` (default loader) for this test rather
    than reusing one a sibling test wired with an injected stub loader.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=HarborConnector,
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
    """Target satisfying both ``HarborTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        # ``Connector.supported_version_range`` is ``">=2.0,<3.0"``; the
        # resolver parses target.fingerprint.version as PEP 440, so use a
        # concrete dotted version (not the wildcard ``"2.x"`` triple
        # version that names the connector key but isn't PEP-440 parseable).
        self.fingerprint = type("_FP", (), {"version": "2.11.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0a2")
        self.name = "harbor-credread"
        self.host = _HARBOR_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/harbor-credread"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread-harbor",
        name="Harbor Cred Read Operator",
        email=None,
        raw_jwt="op.credread.harbor.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a2"),
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
        summary="Read Harbor system info",
        description="Return the Harbor general system information.",
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
    """The full dispatch->loader->Harbor chain returns status="ok" with no injected loader."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary creds via the
    # real ``load_basic_credentials`` -> ``vault_client_for_operator``
    # path. No credentials_loader is injected anywhere — the resolver
    # builds ``HarborConnector()`` so the default loader runs.
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_HARBOR_BASE_URL, assert_all_called=False) as mock:
        op_route = mock.get("/api/v2.0/systeminfo").respond(200, json=_OP_RESPONSE)

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
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.harbor.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The read op hit the mocked Harbor instance with HTTP Basic auth.
    assert op_route.called and op_route.call_count == 1
    sent_auth = op_route.calls[0].request.headers.get("authorization")
    assert sent_auth is not None and sent_auth.startswith("Basic ")
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
        async with respx.mock(base_url=_HARBOR_BASE_URL, assert_all_called=False) as mock:
            mock.get("/api/v2.0/systeminfo").respond(200, json=_OP_RESPONSE)

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
