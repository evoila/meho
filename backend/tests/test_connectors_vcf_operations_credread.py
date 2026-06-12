# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the vROps live credential-read chain (G3.10-T2 #946).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> VcfOperationsConnector()   # default loader
      -> dispatch_ingested -> auth_headers
      -> load_credentials_from_vault                # the live shared loader
      -> load_basic_credentials (G3.9-T2)           # operator-context Vault read
      -> GET /suite-api/api/versions/current        # the actual read op (Basic auth)
      -> OperationResult(status="ok")

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane** (``pytest -n auto`` /
``--ignore=tests/integration``) with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against canned creds. The
  default loader is exercised verbatim — *not* an injected stub. This is
  the load-bearing difference from ``test_connectors_vcf_operations_auth.py``
  (which injects ``_stub_loader``): here the connector is constructed by
  the resolver as ``VcfOperationsConnector()`` with no
  ``credentials_loader``, so the default
  :func:`~meho_backplane.connectors._shared.vcf_auth.load_credentials_from_vault`
  runs.
* **vROps** — respx replays ``GET /suite-api/api/versions/current``
  (returns a canned version payload). No network.

The vROps appliance uses HTTP Basic on every request (stateless — no
session token; vROps has no ``/api/session`` analogue). So unlike the
vmware-rest precedent there is no session establish call to mock; the
single read op carries the Vault-read credentials in
``Authorization: Basic <b64>``.

Why this lives in the unit lane (not ``tests/integration``)
===========================================================

The acceptance criterion requires this E2E to run in the default
``pytest -n auto`` sweep — which deselects ``tests/integration`` (the
container/PG lane). The recorded-fixture replay needs neither a real
Vault nor a real vROps (both stubbed at their boundaries) and only the
autouse SQLite ``endpoint_descriptor`` schema the top-level conftest
already migrates per worker. Mirrors
``test_connectors_vmware_rest_credread.py``'s layout.
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
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors._shared.vcf_auth import CredentialsCache
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_operations import VcfOperationsConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER appear in the result or logs.
# Generated nowhere near a real secret; these strings are the leak canaries.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "svc-vrops-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread-vrops"

#: The connector triple ``vrops-rest-9.0`` decodes to.
_PRODUCT_DESCRIPTOR = "vrops"  # parse_connector_id splits at first hyphen
_PRODUCT_TARGET = "vcf-operations"  # the registered connector class key
_VERSION = "9.0"
_IMPL_ID = "vrops-rest"
_CONNECTOR_ID = "vrops-rest-9.0"

#: A spec-relative ingested GET op. ``/suite-api/api/versions/current`` is
#: the canonical version-read op vROps documents.
_OP_ID = "GET:/suite-api/api/versions/current"
_OP_PATH = "/suite-api/api/versions/current"

#: respx base URL the target points at. Port 443 keeps ``_base_url`` from
#: appending ``:port`` so the router matches the connector's client URL.
#: ``.test.invalid`` (RFC 6761) guarantees no real egress.
_VROPS_HOST = "vrops-credread.test.invalid"
_VROPS_BASE_URL = f"https://{_VROPS_HOST}"

#: The read op's response payload — vROps reports ``releaseName`` /
#: ``buildNumber`` on the canonical version endpoint.
_OP_RESPONSE: dict[str, Any] = {
    "releaseName": "9.0.0",
    "buildNumber": 23456789,
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
    fresh ``VcfOperationsConnector()`` (default loader) for this test rather
    than reusing one a sibling test wired with an injected stub loader.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT_TARGET,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=VcfOperationsConnector,
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
    """Target satisfying both ``VcfOperationsTargetLike`` and the resolver shape.

    Carries the resolver attributes (``product`` / ``fingerprint`` /
    ``preferred_impl_id`` / ``id``) plus the vROps-loader attributes
    (``name`` / ``host`` / ``port`` / ``secret_ref`` / ``auth_model`` /
    ``auth_source``). ``secret_ref`` is the **logical** KV-v2 path the
    live default loader reads — relative to the mount root, no
    ``secret/`` prefix and no ``/data/`` segment (hvac inserts
    ``/data/`` itself).
    """

    def __init__(self) -> None:
        self.product = _PRODUCT_TARGET
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0a1")
        self.name = "vrops-credread"
        self.host = _VROPS_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/vrops-credread"
        self.auth_model = "shared_service_account"
        self.auth_source: str | None = None


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread-vrops",
        name="Cred Read vROps Operator",
        email=None,
        raw_jwt="op.credread.vrops.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
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
        summary="Current vROps version",
        description="Returns releaseName + buildNumber for the vROps appliance.",
        tags=["fingerprint"],
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
    """The full dispatch->loader->vROps chain returns status="ok" with no injected loader."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary creds via the
    # real ``load_basic_credentials`` -> ``vault_client_for_operator``
    # path. No credentials_loader is injected anywhere — the resolver
    # builds ``VcfOperationsConnector()`` so the default shared loader runs.
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=False) as mock:
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
    assert isinstance(result.result, dict)
    assert result.result == _OP_RESPONSE

    # The default loader actually read Vault under the operator's identity.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.vrops.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The op route was hit, and the Basic-auth header carried the read creds.
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
        async with respx.mock(base_url=_VROPS_BASE_URL, assert_all_called=False) as mock:
            mock.get(_OP_PATH).respond(200, json=_OP_RESPONSE)

            result = await dispatch(
                operator=operator,
                connector_id=_CONNECTOR_ID,
                op_id=_OP_ID,
                target=target,
                params={},
            )

    assert result.status == "ok", result.error

    # The credential never rides the OperationResult.
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


@pytest.mark.asyncio
async def test_credentials_cache_fast_path_fails_closed_on_empty_raw_jwt() -> None:
    """CredentialsCache.get rejects an empty operator.raw_jwt before the cache lookup.

    Exercises the defense-in-depth guard the shared cache enforces in
    addition to each consuming connector's auth_headers boundary check.
    Primes the cache via a normal first read under an authenticated
    operator (using an injected stub loader, no Vault round-trip), then
    issues a second call with raw_jwt="" against the SAME target and
    asserts the cache raises VaultCredentialsReadError without returning
    the primed entry. Mirrors the loader-path guard at
    vault_creds._resolve_secret_ref so a future regression in any
    consumer's auth_headers boundary check cannot open a silent
    cache-hit path. See docs/architecture/connector-auth.md §
    "Cache scoping under shared_service_account".
    """
    loader_calls = 0

    async def _stub_loader(target: Any, operator: Operator) -> dict[str, str]:
        nonlocal loader_calls
        loader_calls += 1
        return {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}

    cache = CredentialsCache(_stub_loader, product_label="vrops")
    target = _CredReadTarget()

    primed = await cache.get(target, _make_operator())
    assert primed == {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    assert loader_calls == 1

    system_operator = Operator(
        sub="system",
        name="System",
        email=None,
        raw_jwt="",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )
    with pytest.raises(VaultCredentialsReadError) as exc_info:
        await cache.get(target, system_operator)

    assert target.name in str(exc_info.value)
    assert "operator" in str(exc_info.value).lower()
    # Loader was not re-invoked; the guard ran ahead of the lookup.
    assert loader_calls == 1
