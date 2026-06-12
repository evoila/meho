# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture E2E for the vcf-automation dual-plane credread chain (G3.10-T3 #947).

Proves the **full** dual-plane dispatch chain end to end with the real
(default, non-injected) credential loader:

    dispatch(...)
      -> _resolve_connector_instance -> VcfAutomationConnector()  # default loader
      -> dispatch_ingested -> _request_json (path-aware)
      -> _provider_session_token  /  _tenant_session_token
      -> load_credentials_with_override (operator threaded)
      -> load_credentials_from_vault                # the live loader
      -> load_basic_credentials (G3.9-T2)           # operator-context Vault read
      -> POST /cloudapi/1.0.0/sessions/provider     # HTTP Basic w/ read creds
            -> X-VMWARE-VCLOUD-ACCESS-TOKEN JWT
      -> POST /iaas/api/login                       # JSON body w/ read creds
            -> {"token": ...}
      -> GET <provider-op path>  +  GET <tenant-op path>   # the actual reads
      -> OperationResult(status="ok")

The two "real" leaves are stubbed at their boundaries so the gate runs
in the **secret-free unit lane** (``pytest -n auto`` /
``--ignore=tests/integration``) with no Docker and no real secret:

* **Vault** — the in-process fake (``install_fake_client``) patches
  ``meho_backplane.auth.vault._build_client`` so the helper's real
  JWT/OIDC login + KV-v2 read code path runs against canned creds. The
  default loader is exercised verbatim — *not* an injected stub. This is
  the load-bearing difference from
  ``test_connectors_vcf_automation_auth.py``
  (which injects ``_stub_loader``) and
  ``test_connectors_vcf_automation_e2e.py``
  (which injects ``_vcfa_credentials_loader`` to keep its dispatch
  acceptance tests focused on the dual-plane routing): here the
  connector is constructed by the resolver as
  ``VcfAutomationConnector()`` with no ``credentials_loader``, so the
  default ``load_credentials_from_vault`` runs.
* **VCFA appliance** — respx replays the per-plane login endpoints + a
  provider-plane read op + a tenant-plane read op. No network.

No credential value and no JWT bearer token may appear in any captured
log event or any field of the returned ``OperationResult``.
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
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_automation import VcfAutomationConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential + JWT values — asserted to NEVER appear in the result
# or any log event. Generated nowhere near a real secret; these strings
# exist purely as leak canaries.
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "svc-vcfa-canary"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-credread-vcfa"
_CANARY_PROVIDER_JWT = "vcfa-credread-provider-jwt-canary"
_CANARY_TENANT_TOKEN = "vcfa-credread-tenant-token-canary"

#: The connector triple ``vcfa-rest-9.0`` decodes to. ``parse_connector_id``
#: pulls ``"vcfa"`` as the product slug; the connector class registers under
#: the wider ``"vcf-automation"`` slug (same shape as sddc-manager / sddc).
_DESCRIPTOR_PRODUCT = "vcfa"
_CONNECTOR_PRODUCT = "vcf-automation"
_VERSION = "9.0"
_IMPL_ID = "vcfa-rest"
_CONNECTOR_ID = "vcfa-rest-9.0"

#: Two ingested ops -- one per plane -- so the read chain exercises both
#: login flows and both Bearer-token shapes.
_PROVIDER_OP_ID = "GET:/cloudapi/1.0.0/site"
_PROVIDER_OP_PATH = "/cloudapi/1.0.0/site"
_TENANT_OP_ID = "GET:/iaas/api/about"
_TENANT_OP_PATH = "/iaas/api/about"

#: respx base URL the target points at. ``.test.invalid`` (RFC 6761) so no
#: real egress can fire even if respx-routing breaks.
_VCFA_FQDN = "vcfa-credread.test.invalid"
_VCFA_BASE_URL = f"https://{_VCFA_FQDN}"

#: Recorded-fixture payloads — minimal but valid VCFA dual-plane responses.
_PROVIDER_SITE_PAYLOAD: dict[str, Any] = {
    "id": "site-credread",
    "name": "VCFA-CREDREAD",
    "productVersion": "9.0.0.0-credread",
}
_TENANT_ABOUT_PAYLOAD: dict[str, Any] = {
    "latestApiVersion": "2024-01-01",
    "supportedApis": [{"apiVersion": "2024-01-01"}],
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
    fresh ``VcfAutomationConnector()`` (default loader) for this test
    rather than reusing one a sibling test wired with an injected stub
    loader.
    """
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_CONNECTOR_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=VcfAutomationConnector,
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub for the seeded descriptor rows."""
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
    """Target satisfying both ``VcfAutomationTargetLike`` and the resolver shape.

    Carries the resolver attributes (``product`` / ``fingerprint`` /
    ``preferred_impl_id`` / ``id``) plus the loader attributes
    (``name`` / ``host`` / ``port`` / ``secret_ref`` / ``auth_model`` /
    ``fqdn`` / ``domain`` / ``provider_username`` /
    ``provider_secret_ref``). ``secret_ref`` is the **logical** KV-v2
    path the live default loader reads — relative to the mount root,
    no ``secret/`` prefix and no ``/data/`` segment (hvac inserts
    ``/data/`` itself). This is the exact shape an operator stores per
    the future vcf-automation onboarding doc.
    """

    def __init__(self) -> None:
        self.product = _CONNECTOR_PRODUCT
        # ``resolve_connector`` reads ``fingerprint.version`` via attribute
        # access; a tiny duck-typed stand-in keeps the test free of the
        # full Pydantic FingerprintResult shape.
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        # Tenant-unique cache key component (#1642/#1672); without it
        # ``target_cache_key`` would raise AttributeError at runtime.
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-00000000a0fa")
        self.name = "vcfa-credread-target"
        self.host = _VCFA_FQDN
        self.port = 443
        # FQDN already equals host, so vhost routing collapses to the
        # plain URL; ``compose_base_url`` accepts a None ``fqdn`` when
        # ``host`` is itself a hostname (not an IP).
        self.fqdn: str | None = None
        self.domain: str | None = None
        self.provider_username: str | None = None
        self.provider_secret_ref: str | None = None
        self.secret_ref = "targets/op-credread/vcfa-credread"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-vcfa-credread",
        name="VCFA Cred Read Operator",
        email=None,
        raw_jwt="op.vcfa.credread.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0fa"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_descriptors(session: AsyncSession, embedding: list[float]) -> None:
    """Insert two enabled ingested GET descriptors -- one per plane.

    The descriptor rows use the ``"vcfa"`` product slug
    :func:`parse_connector_id` derives from ``"vcfa-rest-9.0"``; the
    connector class registers under the wider ``"vcf-automation"`` slug
    (same dual-slug shape sddc-manager established).
    """
    now = datetime.now(UTC)
    for op_id, method, path, summary in (
        (_PROVIDER_OP_ID, "GET", _PROVIDER_OP_PATH, "Provider site root"),
        (_TENANT_OP_ID, "GET", _TENANT_OP_PATH, "Tenant about (versions)"),
    ):
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product=_DESCRIPTOR_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                op_id=op_id,
                source_kind="ingested",
                method=method,
                path=path,
                handler_ref=None,
                summary=summary,
                description=summary,
                tags=["spec:iaas" if path.startswith("/iaas/api/") else "spec:cloudapi"],
                parameter_schema={"type": "object", "properties": {}},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=embedding,
                custom_description=None,
                custom_notes=None,
                created_at=now,
                updated_at=now,
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_dispatch_executes_full_dual_plane_credread_chain_returns_ok(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full provider+tenant cred-read chain returns status="ok" with no injected loader.

    Exercises:

    1. Default loader runs (no ``credentials_loader=`` injection on
       ``VcfAutomationConnector``).
    2. Operator-context Vault read fires under ``operator.raw_jwt``.
    3. Provider plane: ``POST /cloudapi/1.0.0/sessions/provider`` with
       HTTP Basic auth using the Vault-read creds -> ``X-VMWARE-VCLOUD-
       ACCESS-TOKEN`` JWT cached as ``Authorization: Bearer ...`` on
       the subsequent ``/cloudapi/*`` op.
    4. Tenant plane: ``POST /iaas/api/login`` with JSON body using the
       same Vault-read creds -> ``{"token": ...}`` cached as
       ``Authorization: Bearer ...`` on the subsequent ``/iaas/api/*``
       op.
    5. Both reads return ``status="ok"`` envelopes carrying the
       recorded-fixture payloads.
    """
    await _seed_descriptors(session, stub_embedding_service.encode_one.return_value)

    # Vault leaf: the in-process fake returns the canary creds via the
    # real ``load_basic_credentials`` -> ``vault_client_for_operator``
    # path. No credentials_loader is injected anywhere — the resolver
    # builds ``VcfAutomationConnector()`` so the default loader runs.
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    # ``assert_all_called=False``: the connector instance is owned by
    # the resolver cache, so any aclose teardown fires on a *later*
    # cache reset (after this block closes), not inside it.
    async with respx.mock(base_url=_VCFA_BASE_URL, assert_all_called=False) as mock:
        provider_login = mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": _CANARY_PROVIDER_JWT}
        )
        tenant_login = mock.post("/iaas/api/login").respond(
            200, json={"token": _CANARY_TENANT_TOKEN}
        )
        provider_route = mock.get(_PROVIDER_OP_PATH).respond(200, json=_PROVIDER_SITE_PAYLOAD)
        tenant_route = mock.get(_TENANT_OP_PATH).respond(200, json=_TENANT_ABOUT_PAYLOAD)

        provider_result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=_PROVIDER_OP_ID,
            target=target,
            params={},
        )
        tenant_result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=_TENANT_OP_ID,
            target=target,
            params={},
        )

    # ---- The load-bearing assertion: the chain executed end to end. ----
    assert provider_result.status == "ok", provider_result.error
    assert tenant_result.status == "ok", tenant_result.error
    assert provider_result.result == _PROVIDER_SITE_PAYLOAD
    assert tenant_result.result == _TENANT_ABOUT_PAYLOAD

    # ---- The default loader actually read Vault under the operator. ----
    # Exactly two reads -- one per plane's session-establish (provider
    # uses target.secret_ref, tenant uses the same since
    # provider_secret_ref is unset on the test target).
    assert fake.auth.jwt.login_calls, "Vault JWT/OIDC login did not fire"
    assert fake.auth.jwt.login_calls[-1]["jwt"] == operator.raw_jwt
    assert len(fake.secrets.kv.v2.read_calls) == 2, (
        "expected 2 KV-v2 reads (one per plane session-establish); got "
        f"{fake.secrets.kv.v2.read_calls!r}"
    )
    for read in fake.secrets.kv.v2.read_calls:
        assert read["path"] == target.secret_ref

    # ---- Both plane logins + both read ops hit the mocked appliance. ----
    assert provider_login.called and provider_login.call_count == 1
    assert tenant_login.called and tenant_login.call_count == 1
    assert provider_route.called and provider_route.call_count == 1
    assert tenant_route.called and tenant_route.call_count == 1

    # ---- Provider login carried HTTP Basic (header present, opaque). ----
    sent_auth = provider_login.calls[0].request.headers.get("authorization")
    assert sent_auth is not None and sent_auth.startswith("Basic ")

    # ---- One audit/broadcast per dispatched op. ----
    assert len(captured_events) == 2


@pytest.mark.asyncio
async def test_credread_chain_never_leaks_credential_or_jwt_in_result_or_logs(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    captured_events: list[BroadcastEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential value and no Bearer JWT appears in OperationResult or any captured log.

    Asserts the no-secret-in-logs invariant the locked decision in
    ``docs/architecture/connector-auth.md`` requires. The grep target
    set covers:

    * the canary username + password (the KV-v2 secret values),
    * the provider-plane JWT (the ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` value),
    * the tenant-plane bearer token (the ``token`` field value),
    * the operator's raw JWT.

    Each is asserted absent from:

    * the ``OperationResult`` envelope ``repr`` (result / error / extras),
    * every structlog event captured during the dispatch chain
      (loader / connector / dispatcher / audit / broadcast),
    * the broadcast event payload list.
    """
    await _seed_descriptors(session, stub_embedding_service.encode_one.return_value)

    install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )

    target = _CredReadTarget()
    operator = _make_operator()

    with capture_logs() as captured:
        async with respx.mock(base_url=_VCFA_BASE_URL, assert_all_called=False) as mock:
            mock.post("/cloudapi/1.0.0/sessions/provider").respond(
                200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": _CANARY_PROVIDER_JWT}
            )
            mock.post("/iaas/api/login").respond(200, json={"token": _CANARY_TENANT_TOKEN})
            mock.get(_PROVIDER_OP_PATH).respond(200, json=_PROVIDER_SITE_PAYLOAD)
            mock.get(_TENANT_OP_PATH).respond(200, json=_TENANT_ABOUT_PAYLOAD)

            provider_result = await dispatch(
                operator=operator,
                connector_id=_CONNECTOR_ID,
                op_id=_PROVIDER_OP_ID,
                target=target,
                params={},
            )
            tenant_result = await dispatch(
                operator=operator,
                connector_id=_CONNECTOR_ID,
                op_id=_TENANT_OP_ID,
                target=target,
                params={},
            )

    assert provider_result.status == "ok", provider_result.error
    assert tenant_result.status == "ok", tenant_result.error

    secrets_to_check: tuple[str, ...] = (
        _CANARY_PASSWORD,
        _CANARY_USERNAME,
        _CANARY_PROVIDER_JWT,
        _CANARY_TENANT_TOKEN,
        operator.raw_jwt,
    )

    for blob_name, blob in (
        ("provider OperationResult", repr(provider_result)),
        ("tenant OperationResult", repr(tenant_result)),
        ("structlog capture", repr(captured)),
        ("broadcast events", repr(captured_events)),
    ):
        for secret in secrets_to_check:
            assert secret not in blob, (
                f"{secret!r} leaked into {blob_name}; cred / JWT must not "
                "appear in result envelopes or log events. Blob excerpt: "
                f"{blob[:400]!r}"
            )


@pytest.mark.asyncio
async def test_session_token_fast_paths_fail_closed_on_empty_raw_jwt() -> None:
    """Both vcf-automation cache fast-paths reject empty raw_jwt before lookup.

    Exercises the defense-in-depth guard on both plane caches
    (_provider_session_token + _tenant_session_token) in addition to
    auth_headers' boundary check. Primes each plane's cache via a
    normal first session-establish under an authenticated operator
    (respx mocks the per-plane login endpoints and an injected loader
    returns canned creds — no Vault round-trip), then invokes each
    method again with raw_jwt="" against the SAME target and asserts
    VaultCredentialsReadError without returning the cached token.
    Mirrors the loader-path guard and the sibling check in
    CredentialsCache.get / vcf_logs._session_token so a future
    regression in auth_headers cannot leak a cached provider JWT or
    tenant token to an unauthenticated caller. See
    docs/architecture/connector-auth.md § "Cache scoping under
    shared_service_account".
    """

    async def _stub_loader(target: Any, operator: Operator) -> dict[str, str]:
        return {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}

    connector = VcfAutomationConnector(credentials_loader=_stub_loader)
    target = _CredReadTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_VCFA_BASE_URL, assert_all_called=False) as mock:
        mock.post("/cloudapi/1.0.0/sessions/provider").respond(
            200, headers={"X-VMWARE-VCLOUD-ACCESS-TOKEN": _CANARY_PROVIDER_JWT}
        )
        mock.post("/iaas/api/login").respond(200, json={"token": _CANARY_TENANT_TOKEN})

        primed_provider = await connector._provider_session_token(target, operator)
        primed_tenant = await connector._tenant_session_token(target, operator)

    assert primed_provider == _CANARY_PROVIDER_JWT
    assert primed_tenant == _CANARY_TENANT_TOKEN
    cache_key = target_cache_key(target)
    assert connector._provider_tokens[cache_key] == _CANARY_PROVIDER_JWT
    assert connector._tenant_tokens[cache_key] == _CANARY_TENANT_TOKEN

    system_operator = Operator(
        sub="system",
        name="System",
        email=None,
        raw_jwt="",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0fa"),
        tenant_role=TenantRole.OPERATOR,
    )

    with pytest.raises(VaultCredentialsReadError) as exc_provider:
        await connector._provider_session_token(target, system_operator)
    assert target.name in str(exc_provider.value)
    assert "operator" in str(exc_provider.value).lower()

    with pytest.raises(VaultCredentialsReadError) as exc_tenant:
        await connector._tenant_session_token(target, system_operator)
    assert target.name in str(exc_tenant.value)
    assert "operator" in str(exc_tenant.value).lower()

    # Both caches still hold the primed tokens; the guards ran ahead
    # of any cache mutation.
    assert connector._provider_tokens[cache_key] == _CANARY_PROVIDER_JWT
    assert connector._tenant_tokens[cache_key] == _CANARY_TENANT_TOKEN

    await connector.aclose()
