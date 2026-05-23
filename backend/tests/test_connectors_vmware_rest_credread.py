# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the vmware-rest live credential-read chain (G3.9-T3 #942).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> VmwareRestConnector()   # default loader
      -> dispatch_ingested -> mount_op_path -> _session_token
      -> load_session_credentials_from_vault            # the live loader
      -> load_basic_credentials (G3.9-T2)               # operator-context Vault read
      -> POST /api/session (HTTP basic with the read creds)
      -> GET <op path>                                  # the actual read op
      -> OperationResult(status="ok")

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane** (``pytest -n auto`` /
``--ignore=tests/integration``) with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against canned creds. The
  default loader is exercised verbatim — *not* an injected stub. This is
  the load-bearing difference from ``test_connectors_vmware_rest_auth.py``
  (which injects ``_stub_loader``): here the connector is constructed by
  the resolver as ``VmwareRestConnector()`` with no ``session_loader``,
  so the default ``load_session_credentials_from_vault`` runs.
* **vCenter** — respx replays ``POST /api/session`` (returns the session
  token) and the op ``GET`` (returns the read payload). No network.

The live lab-vCenter run (real operator JWT against a real vCenter +
Vault) is the env-gated opt-in smoke in
``tests/integration/test_connectors_vmware_rest_lab_smoke.py``; it skips
cleanly in CI.

Why this lives in the unit lane (not ``tests/integration``)
===========================================================

The acceptance criterion requires this E2E to run in the default
``pytest -n auto`` sweep — which deselects ``tests/integration`` (the
container/PG lane). The recorded-fixture replay needs neither a real
Vault nor a real vCenter (both stubbed at their boundaries) and only the
autouse SQLite ``endpoint_descriptor`` schema the top-level conftest
already migrates per worker. So it belongs here, mirroring the T2 split:
unit ``test_connectors_vault_creds.py`` (fake) + integration
``test_connectors_vault_creds_dev_e2e.py`` (live Vault). The live vendor
round-trip is the integration-lane smoke.
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
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER appear in the result or logs.
# Generated nowhere near a real secret; these strings are the leak canaries.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "svc-vsphere-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread"

#: The connector triple ``vmware-rest-9.0`` decodes to.
_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"
_CONNECTOR_ID = "vmware-rest-9.0"

#: A spec-relative ingested GET op. ``mount_op_path`` prepends ``/api``
#: (the modern mount the session establish selected) at dispatch time.
_OP_ID = "GET:/vcenter/vm"
_OP_PATH = "/vcenter/vm"

#: respx base URL the target points at. Port 443 keeps ``_base_url`` from
#: appending ``:port`` so the router matches the connector's client URL.
#: ``.test.invalid`` (RFC 6761) guarantees no real egress.
_VCENTER_HOST = "vcenter-credread.test.invalid"
_VCENTER_BASE_URL = f"https://{_VCENTER_HOST}"
_SESSION_TOKEN = "credread-session-token"

#: The read op's response payload — set-shaped but small enough that the
#: pass-through reducer returns it inline (no handle); shape doesn't matter
#: for this test, only that ``status="ok"`` round-trips.
_OP_RESPONSE: dict[str, Any] = {"vms": [{"vm": "vm-101", "name": "demo-vm"}]}


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
    fresh ``VmwareRestConnector()`` (default loader) for this test rather
    than reusing one a sibling test wired with an injected stub loader.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=VmwareRestConnector,
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
    """Target satisfying both ``VsphereTargetLike`` and the resolver shape.

    Carries the resolver attributes (``product`` / ``fingerprint`` /
    ``preferred_impl_id`` / ``id``) plus the vmware-loader attributes
    (``name`` / ``host`` / ``port`` / ``secret_ref`` / ``auth_model``).
    ``secret_ref`` is the **logical** KV-v2 path the live default loader
    reads — relative to the mount root, no ``secret/`` prefix and no
    ``/data/`` segment (hvac inserts ``/data/`` itself). This is the
    exact shape an operator stores per
    ``docs/cross-repo/vmware-rest-onboarding.md``.
    """

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "vcenter-credread"
        self.host = _VCENTER_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/vcenter-credread"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread",
        name="Cred Read Operator",
        email=None,
        raw_jwt="op.credread.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
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
        summary="List VMs",
        description="List virtual machines in the vCenter inventory.",
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
    """The full dispatch→loader→vCenter chain returns status="ok" with no injected loader."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary creds via the
    # real ``load_basic_credentials`` -> ``vault_client_for_operator``
    # path. No session_loader is injected anywhere — the resolver builds
    # ``VmwareRestConnector()`` so the default loader runs.
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    # ``assert_all_called=False``: the connector instance is owned by the
    # resolver cache, so its ``aclose`` DELETE revoke fires on a *later*
    # cache reset (after this block closes), not inside it. Registering
    # the DELETE keeps any in-block teardown handled without forcing a
    # call-count on a route this dispatch path doesn't reach. Same stance
    # as ``tests/integration/test_connectors_vmware_rest_vcsim.py``.
    async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
        session_route = mock.post("/api/session").respond(200, json=_SESSION_TOKEN)
        op_route = mock.get("/api/vcenter/vm").respond(200, json=_OP_RESPONSE)
        mock.delete("/api/session").respond(204)

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
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The session establish + the read op both hit the mocked vCenter.
    assert session_route.called and session_route.call_count == 1
    assert op_route.called and op_route.call_count == 1
    # Basic auth carried the Vault-read creds (header present, opaque).
    sent_auth = session_route.calls[0].request.headers.get("authorization")
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
        async with respx.mock(base_url=_VCENTER_BASE_URL, assert_all_called=False) as mock:
            mock.post("/api/session").respond(200, json=_SESSION_TOKEN)
            mock.get("/api/vcenter/vm").respond(200, json=_OP_RESPONSE)
            mock.delete("/api/session").respond(204)

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
