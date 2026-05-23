# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the sddc-manager live credential-read chain (G3.10-T1 #945).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> SddcManagerConnector()   # default loader
      -> dispatch_ingested -> _request_json -> auth_headers
      -> _load_credentials -> load_credentials_from_vault        # the live loader
      -> load_basic_credentials (#941)                           # operator-context Vault read
      -> GET <op path> (HTTP Basic auth w/ username@sso_realm)
      -> OperationResult(status="ok")

SDDC Manager differs from nsx/vmware in that there is no session
establish call — HTTP Basic auth rides every request. The username is
formatted as ``username@sso_realm`` (default ``vsphere.local``) before
the base64 encode; the password is the Vault-read value verbatim. The
credential read still goes through the shared operator-context Vault
helper.

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane** (``pytest -n auto`` /
``--ignore=tests/integration``) with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against canned creds. The
  default loader is exercised verbatim — *not* an injected stub.
* **SDDC Manager** — respx replays the op ``GET`` (returns the read
  payload). No network.

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
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.sddc_manager import SddcManagerConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER appear in the result or logs.
# Generated nowhere near a real secret; these strings are the leak canaries.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "svc-sddc-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread-sddc"

#: The connector triple ``sddc-rest-9.0`` decodes to. Note the asymmetry:
#: the v2 registry uses ``product="sddc-manager"`` (and so does the
#: target row + the resolver), but ``parse_connector_id("sddc-rest-9.0")``
#: returns ``product="sddc"`` — the descriptor + dispatcher leg uses that
#: form. See sddc_manager/core_ops.py docstring §SDDC_PRODUCT for the
#: locked rationale.
_REGISTRY_PRODUCT = "sddc-manager"
_DESCRIPTOR_PRODUCT = "sddc"
_VERSION = "9.0"
_IMPL_ID = "sddc-rest"
_CONNECTOR_ID = "sddc-rest-9.0"

#: A spec-relative ingested GET op against SDDC Manager's primary endpoint.
_OP_ID = "GET:/v1/sddc-managers"
_OP_PATH = "/v1/sddc-managers"

#: respx base URL the target points at. Port 443 keeps ``_base_url`` from
#: appending ``:port`` so the router matches the connector's client URL.
_SDDC_HOST = "sddc-credread.test.invalid"
_SDDC_BASE_URL = f"https://{_SDDC_HOST}"

#: The read op's response payload — shaped to look like a real SDDC Manager response.
_OP_RESPONSE: dict[str, Any] = {
    "elements": [
        {
            "id": "sddc-credread-id",
            "fqdn": _SDDC_HOST,
            "version": "9.0.0.0",
            "build": "credread-build",
            "domain": {"id": "mgmt-dom", "name": "mgmt"},
        }
    ],
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
    fresh ``SddcManagerConnector()`` (default loader) for this test
    rather than reusing one a sibling test wired with an injected stub
    loader.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_REGISTRY_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=SddcManagerConnector,
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
    """Target satisfying both ``SddcTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        # Target rows use the v2 registry product ``"sddc-manager"`` — the
        # resolver matches ``Connector.product`` against this. The descriptor
        # row (seeded below) uses ``"sddc"`` (from ``parse_connector_id``).
        self.product = _REGISTRY_PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "sddc-credread"
        self.host = _SDDC_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/sddc-credread"
        self.auth_model = "shared_service_account"
        self.sso_realm = "vsphere.local"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread-sddc",
        name="SDDC Cred Read Operator",
        email=None,
        raw_jwt="op.credread.sddc.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a3"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_descriptor(session: AsyncSession, embedding: list[float]) -> None:
    """Insert one enabled ingested GET descriptor for the read op."""
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        # Descriptor uses ``"sddc"`` (parse_connector_id of "sddc-rest-9.0");
        # the connector class + target row both use ``"sddc-manager"``.
        product=_DESCRIPTOR_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_OP_ID,
        source_kind="ingested",
        method="GET",
        path=_OP_PATH,
        handler_ref=None,
        summary="Read SDDC Manager inventory",
        description="Return the SDDC Manager appliance(s) under management.",
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
    """The full dispatch->loader->SDDC chain returns status="ok" with no injected loader."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary creds via the
    # real ``load_basic_credentials`` -> ``vault_client_for_operator``
    # path. No credentials_loader is injected anywhere — the resolver
    # builds ``SddcManagerConnector()`` so the default loader runs.
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        op_route = mock.get("/v1/sddc-managers").respond(200, json=_OP_RESPONSE)

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
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.sddc.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The read op hit the mocked SDDC Manager with HTTP Basic auth.
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
        async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
            mock.get("/v1/sddc-managers").respond(200, json=_OP_RESPONSE)

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
