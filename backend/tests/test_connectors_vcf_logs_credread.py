# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the vRLI live credential-read chain (G3.10-T2 #946).

Proves the **full** dispatch chain end to end with a real (default,
non-injected) credential loader, including vRLI's session-establish
round-trip:

    dispatch(...)
      -> _resolve_connector_instance -> VcfLogsConnector()   # default loader
      -> dispatch_ingested -> auth_headers -> _session_token
      -> load_credentials_from_vault           # the live shared loader
      -> load_basic_credentials (G3.9-T2)      # operator-context Vault read
      -> POST /api/v2/sessions                 # session establish (JSON body)
      -> GET <op path>                         # the actual read op (Bearer)
      -> OperationResult(status="ok")

A second test covers the 401-on-downstream → invalidate session →
re-login (which re-reads creds from the cache, not Vault — credentials
are cached per target across the lifetime of the connector instance)
→ retry-once success path.

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane**:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against canned creds. The
  default loader is exercised verbatim — *not* an injected stub.
* **vRLI** — respx replays ``POST /api/v2/sessions`` (returns the
  ``sessionId`` JSON body) and the op ``GET`` (returns the read
  payload). No network.

The same shape as the vmware-rest precedent
(``test_connectors_vmware_rest_credread.py``), differing only in the
session-establish payload shape (JSON ``{username, password, provider}``
returning ``{sessionId, ttl}``) and the absence of a DELETE-revoke on
``aclose`` (vRLI doesn't issue revoke — same posture NSX takes).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_logs import VcfLogsConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER appear in the result or logs.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "svc-vrli-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread-vrli"

#: The connector triple ``vrli-rest-9.0`` decodes to. Since G0.26-T4
#: (#1798) aligned VcfLogsConnector to ``product="vrli"``, the descriptor
#: (parser-derived) product and the target / registration product are
#: the same canonical token — no split.
_PRODUCT_DESCRIPTOR = "vrli"
_PRODUCT_TARGET = "vrli"
_VERSION = "9.0"
_IMPL_ID = "vrli-rest"
_CONNECTOR_ID = "vrli-rest-9.0"

#: A spec-relative ingested GET op. ``/api/v2/version`` is the canonical
#: vRLI version-read op.
_OP_ID = "GET:/api/v2/version"
_OP_PATH = "/api/v2/version"

#: respx base URL. ``.test.invalid`` (RFC 6761) guarantees no real egress.
_VRLI_HOST = "vrli-credread.test.invalid"
_VRLI_BASE_URL = f"https://{_VRLI_HOST}"
_SESSION_PATH = "/api/v2/sessions"
_SESSION_TOKEN = "vrli-credread-session-token"
_REFRESH_SESSION_TOKEN = "vrli-credread-refresh-session-token"

#: The read op's response payload — small enough that the pass-through
#: reducer returns it inline (no handle).
_OP_RESPONSE: dict[str, Any] = {
    "version": "9.0.0.0.21761695",
    "releaseName": "VMware Aria Operations for Logs 9.0",
}


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
        cls=VcfLogsConnector,
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
    """Target satisfying both ``VcfLogsTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = _PRODUCT_TARGET
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0a2")
        self.name = "vrli-credread"
        self.host = _VRLI_HOST
        self.port = 443
        self.secret_ref = "targets/op-credread/vrli-credread"
        self.auth_model = "shared_service_account"
        self.provider: str | None = None  # defaults to "Local" in connector


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-credread-vrli",
        name="Cred Read vRLI Operator",
        email=None,
        raw_jwt="op.credread.vrli.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a2"),
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
        summary="vRLI version",
        description="Returns the vRLI appliance version + release name.",
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
    """The full dispatch->loader->session-login->vRLI chain returns status="ok"."""
    await _seed_descriptor(session, stub_embedding_service.encode_one.return_value)

    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_VRLI_BASE_URL, assert_all_called=False) as mock:
        session_route = mock.post(_SESSION_PATH).respond(
            200, json={"sessionId": _SESSION_TOKEN, "ttl": 1800}
        )
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
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.vrli.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    # The session establish + the read op both hit the mocked vRLI.
    assert session_route.called and session_route.call_count == 1
    assert op_route.called and op_route.call_count == 1
    # The session-establish carried the Vault-read creds in the JSON body
    # (defensive — the auth contract is tested in
    # ``test_connectors_vcf_logs_auth.py``; we only assert here that the
    # session route was hit with the canary username so the chain
    # provably used the Vault-loaded credentials).
    import json as _json

    body = _json.loads(session_route.calls[0].request.read().decode())
    assert body["username"] == _CANARY_USERNAME
    assert body["password"] == _CANARY_PASSWORD
    assert body["provider"] == "Local"
    # The downstream op carried the Bearer session token returned by login.
    sent_auth = op_route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {_SESSION_TOKEN}"
    # One audit + one broadcast for the dispatched op.
    assert len(captured_events) == 1


@pytest.mark.asyncio
async def test_credread_chain_reconnects_after_session_401_relogin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 on the downstream call triggers re-login + retry-once success.

    Exercises the connector's :meth:`_get_json_with_session_retry`
    directly — the dispatch path goes through the base
    :meth:`HttpConnector._request_json` (no auto-401-retry there;
    tenacity retries connection errors + 5xx, not 401). The 401-retry
    layer is the per-connector consumer's responsibility and lives on
    :class:`VcfLogsConnector` as ``_get_json_with_session_retry``; this
    test wires that surface against the live default loader (no
    injected stub).

    The cached session token expires (simulated as a 401) — the
    connector's loop invalidates the token, re-issues
    ``POST /api/v2/sessions``, and retries the downstream call once.
    Vault is read **once** (credentials are cached across the
    re-login), but the session POST is hit **twice**.
    """
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    login_calls = 0

    def _login_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal login_calls
        login_calls += 1
        token = _SESSION_TOKEN if login_calls == 1 else _REFRESH_SESSION_TOKEN
        return httpx.Response(200, json={"sessionId": token, "ttl": 1800})

    downstream_calls = 0

    def _downstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal downstream_calls
        downstream_calls += 1
        # First downstream call 401s (simulated session expiry); second succeeds.
        # We also assert the second call carried the refreshed token.
        if downstream_calls == 1:
            return httpx.Response(401, json={"errorMessage": "session_expired"})
        assert request.headers.get("authorization") == f"Bearer {_REFRESH_SESSION_TOKEN}"
        return httpx.Response(200, json=_OP_RESPONSE)

    connector = VcfLogsConnector()  # default loader — no injected stub
    try:
        async with respx.mock(base_url=_VRLI_BASE_URL, assert_all_called=False) as mock:
            mock.post(_SESSION_PATH).mock(side_effect=_login_handler)
            mock.get(_OP_PATH).mock(side_effect=_downstream_handler)

            payload = await connector._get_json_with_session_retry(
                target, _OP_PATH, operator=operator
            )
    finally:
        await connector.aclose()

    assert payload == _OP_RESPONSE
    # Two session-login round-trips (initial + re-login), two downstream
    # attempts (401 + success). Vault is read once — credentials are
    # cached across the re-login.
    assert login_calls == 2
    assert downstream_calls == 2
    assert len(fake.secrets.kv.v2.read_calls) == 1


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
        async with respx.mock(base_url=_VRLI_BASE_URL, assert_all_called=False) as mock:
            mock.post(_SESSION_PATH).respond(200, json={"sessionId": _SESSION_TOKEN, "ttl": 1800})
            mock.get(_OP_PATH).respond(200, json=_OP_RESPONSE)

            result = await dispatch(
                operator=operator,
                connector_id=_CONNECTOR_ID,
                op_id=_OP_ID,
                target=target,
                params={},
            )

    assert result.status == "ok", result.error

    result_blob = repr(result)
    assert _CANARY_PASSWORD not in result_blob
    assert _CANARY_USERNAME not in result_blob

    log_blob = repr(captured)
    assert _CANARY_PASSWORD not in log_blob
    assert _CANARY_USERNAME not in log_blob

    events_blob = repr(captured_events)
    assert _CANARY_PASSWORD not in events_blob
    assert _CANARY_USERNAME not in events_blob


@pytest.mark.asyncio
async def test_session_token_fast_path_fails_closed_on_empty_raw_jwt() -> None:
    """VcfLogsConnector._session_token rejects an empty raw_jwt before cache lookup.

    Exercises the defense-in-depth guard the bearer cache enforces in
    addition to auth_headers' boundary check. Primes the cache via a
    normal first session-establish under an authenticated operator
    (respx mocks the vRLI POST /api/v2/sessions and an injected loader
    returns canned creds — no Vault round-trip), then invokes
    _session_token again with raw_jwt="" against the SAME target and
    asserts VaultCredentialsReadError without returning the cached
    bearer. Mirrors the loader-path guard and the sibling check in
    CredentialsCache.get so a future regression in auth_headers cannot
    leak a cached bearer to an unauthenticated caller. See
    docs/architecture/connector-auth.md § "Cache scoping under
    shared_service_account".
    """

    async def _stub_loader(target: Any, operator: Operator) -> dict[str, str]:
        return {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}

    connector = VcfLogsConnector(credentials_loader=_stub_loader)
    target = _CredReadTarget()

    async with respx.mock(base_url=_VRLI_BASE_URL, assert_all_called=False) as mock:
        mock.post(_SESSION_PATH).respond(200, json={"sessionId": _SESSION_TOKEN, "ttl": 1800})
        primed = await connector._session_token(target, _make_operator())
    assert primed == _SESSION_TOKEN
    assert connector._session_tokens[target_cache_key(target)] == _SESSION_TOKEN

    system_operator = Operator(
        sub="system",
        name="System",
        email=None,
        raw_jwt="",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a2"),
        tenant_role=TenantRole.OPERATOR,
    )
    with pytest.raises(VaultCredentialsReadError) as exc_info:
        await connector._session_token(target, system_operator)

    assert target.name in str(exc_info.value)
    assert "operator" in str(exc_info.value).lower()
    # The cache still holds the primed token; the guard ran ahead of
    # any cache mutation.
    assert connector._session_tokens[target_cache_key(target)] == _SESSION_TOKEN

    await connector.aclose()
