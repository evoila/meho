# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatch-level proof of the SDDC Manager token session (#2290).

Seeds a ``source_kind='ingested'`` :class:`EndpointDescriptor` for the sddc
triple and dispatches it against a respx-mocked SDDC Manager, proving the
ingested catalog is dispatchable end to end on the ``session_login_token``
flow:

* cold dispatch mints a token at ``POST /v1/tokens`` (JSON body), and every
  op request carries ``Authorization: Bearer <accessToken>`` — never HTTP
  Basic;
* a mid-session ``401`` recovers via exactly one invalidate + re-login + retry
  through the #2067 dispatcher seam, with no restart;
* a second consecutive ``401`` resolves to ``connector_auth_failed`` with no
  retry loop;
* two same-named targets in different tenants never share a token (#1642).

Also pins the module-constant ``SDDC_EXECUTION_PROFILE`` to the shipped
``sddc_manager_minimal.yaml`` catalog profile so the two declarations of the
auth scheme cannot drift.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.sddc_manager import (
    SDDC_EXECUTION_PROFILE,
    SddcManagerConnector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.settings import get_settings

_PRODUCT = "sddc"
_VERSION = "9.0"
_IMPL_ID = "sddc-rest"
_CONNECTOR_ID = "sddc-rest-9.0"

_OP_ID = "GET:/v1/sddc-managers"
_OP_PATH = "/v1/sddc-managers"
_TOKEN_PATH = "/v1/tokens"

_SDDC_HOST = "sddc-session.test.invalid"
_SDDC_BASE_URL = f"https://{_SDDC_HOST}"

_OP_RESPONSE: dict[str, object] = {
    "elements": [
        {
            "id": "sddc-session-id",
            "fqdn": _SDDC_HOST,
            "version": "9.0.0.0",
            "domain": {"id": "mgmt-dom", "name": "mgmt"},
        }
    ],
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around every test."""
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=SddcManagerConnector
    )
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _SessionTarget:
    """Duck-typed target satisfying both ``SddcTargetLike`` and the resolver shape."""

    def __init__(
        self,
        *,
        name: str = "sddc-session",
        host: str = _SDDC_HOST,
        tenant_id: UUID | None = None,
        target_id: UUID | None = None,
    ) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = target_id or uuid.uuid4()
        self.tenant_id: UUID = tenant_id or UUID("00000000-0000-0000-0000-00000000b0b0")
        self.name = name
        self.host = host
        self.port = 443
        self.secret_ref = f"targets/{name}"
        self.auth_model = "shared_service_account"


def _make_operator(tenant_id: UUID) -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-sddc-session",
        name="SDDC Session Operator",
        email=None,
        raw_jwt="op.sddc.session.jwt",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed_descriptor(session: AsyncSession) -> None:
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
        summary="Read SDDC Manager inventory",
        description="Return the SDDC Manager appliance(s) under management.",
        tags=["readiness"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=True,
        embedding=[0.1] * 384,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(descriptor)
    await session.commit()


def _install_stub_loader(username: str = "svc-sddc", password: str = "pw") -> SddcManagerConnector:
    """Resolve + cache the dispatcher's connector instance with a stub loader.

    The dispatcher resolves the class via ``get_or_create_connector_instance``;
    pre-resolving here and patching the loader means the dispatched op uses this
    exact instance (whose token cache we can then inspect) without a Vault read.
    """

    async def _loader(_target: object, _operator: Operator) -> dict[str, str]:
        return {"username": username, "password": password}

    registry = all_connectors_v2()
    instance = get_or_create_connector_instance(registry[(_PRODUCT, _VERSION, _IMPL_ID)])
    instance._credentials_loader = _loader  # type: ignore[attr-defined]
    return instance


@pytest.mark.asyncio
async def test_cold_dispatch_mints_token_then_sends_bearer_never_basic(
    session: AsyncSession,
) -> None:
    """Cold dispatch POSTs /v1/tokens (JSON body) then GETs with Bearer — never Basic."""
    await _seed_descriptor(session)
    instance = _install_stub_loader()
    target = _SessionTarget()

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        token_route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "sess-tok"})
        op_route = mock.get(_OP_PATH).respond(200, json=_OP_RESPONSE)

        result = await dispatch(
            operator=_make_operator(target.tenant_id),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params={},
        )

    assert result.status == "ok", result.error
    # Session mint carried the JSON credential body, no Basic on the login POST.
    assert token_route.call_count == 1
    login_request = token_route.calls[0].request
    assert login_request.headers.get("authorization") is None
    assert json.loads(login_request.content) == {"username": "svc-sddc", "password": "pw"}
    # The op request carried the minted Bearer token — and no request ever Basic.
    assert op_route.call_count == 1
    assert op_route.calls[0].request.headers.get("authorization") == "Bearer sess-tok"
    for call in [*token_route.calls, *op_route.calls]:
        auth = call.request.headers.get("authorization")
        assert auth is None or not auth.startswith("Basic ")
    await instance.aclose()


@pytest.mark.asyncio
async def test_dispatch_recovers_once_from_mid_session_401(session: AsyncSession) -> None:
    """A mid-session 401 recovers via one invalidate + re-login + retry (#2067 seam)."""
    await _seed_descriptor(session)
    instance = _install_stub_loader()
    target = _SessionTarget()

    op_calls = {"n": 0}

    def _op(_request: httpx.Request) -> httpx.Response:
        op_calls["n"] += 1
        if op_calls["n"] == 1:
            return httpx.Response(401, json={"message": "token expired"})
        return httpx.Response(200, json=_OP_RESPONSE)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        token_route = mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "sess-tok"})
        op_route = mock.get(_OP_PATH).mock(side_effect=_op)

        result = await dispatch(
            operator=_make_operator(target.tenant_id),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == _OP_RESPONSE
    # Exactly one retry: two op attempts, two mints (initial + post-invalidation).
    assert op_route.call_count == 2
    assert token_route.call_count == 2
    await instance.aclose()


@pytest.mark.asyncio
async def test_dispatch_second_consecutive_401_is_connector_auth_failed_no_loop(
    session: AsyncSession,
) -> None:
    """A persistent 401 (re-login also 401s) resolves to connector_auth_failed, no loop."""
    await _seed_descriptor(session)
    instance = _install_stub_loader()
    target = _SessionTarget()

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).respond(200, json={"accessToken": "sess-tok"})
        op_route = mock.get(_OP_PATH).respond(401, json={"message": "still bad"})

        result = await dispatch(
            operator=_make_operator(target.tenant_id),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target,
            params={},
        )

    assert result.status == "error"
    assert result.extras is not None
    assert result.extras.get("error_code") == "connector_auth_failed"
    # Exactly one retry — the op was attempted twice, never in a loop.
    assert op_route.call_count == 2
    await instance.aclose()


@pytest.mark.asyncio
async def test_same_name_targets_in_different_tenants_never_share_token(
    session: AsyncSession,
) -> None:
    """Two same-named targets in different tenants dispatch with distinct tokens (#1642)."""
    await _seed_descriptor(session)

    # Loader returns a tenant-specific username; the mock derives the token from
    # it, so a shared token would collapse the two into one string.
    async def _loader(target: _SessionTarget, _operator: Operator) -> dict[str, str]:
        return {"username": f"svc-{target.tenant_id}", "password": "pw"}

    registry = all_connectors_v2()
    instance = get_or_create_connector_instance(registry[(_PRODUCT, _VERSION, _IMPL_ID)])
    instance._credentials_loader = _loader  # type: ignore[attr-defined]

    def _mint(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"accessToken": f"tok-{body['username']}"})

    tenant_a = UUID("00000000-0000-0000-0000-0000000000a1")
    tenant_b = UUID("00000000-0000-0000-0000-0000000000b2")
    target_a = _SessionTarget(name="sddc-shared", tenant_id=tenant_a)
    target_b = _SessionTarget(name="sddc-shared", tenant_id=tenant_b)

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKEN_PATH).mock(side_effect=_mint)
        op_route = mock.get(_OP_PATH).respond(200, json=_OP_RESPONSE)

        result_a = await dispatch(
            operator=_make_operator(tenant_a),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target_a,
            params={},
        )
        result_b = await dispatch(
            operator=_make_operator(tenant_b),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=target_b,
            params={},
        )

    assert result_a.status == "ok" and result_b.status == "ok"
    auth_a = op_route.calls[0].request.headers.get("authorization")
    auth_b = op_route.calls[1].request.headers.get("authorization")
    assert auth_a == f"Bearer tok-svc-{tenant_a}"
    assert auth_b == f"Bearer tok-svc-{tenant_b}"
    assert auth_a != auth_b
    await instance.aclose()


def test_module_profile_matches_shipped_yaml() -> None:
    """The typed connector's ``SDDC_EXECUTION_PROFILE`` equals the shipped catalog profile.

    Guards against the two declarations of the auth scheme drifting: the boot
    stamp reads the YAML, the typed connector reads this constant, and they must
    agree on the ``session_login_token`` scheme.
    """
    import yaml

    from meho_backplane.connectors.profile import ExecutionProfile
    from meho_backplane.operations.ingest import load_profile_resource

    raw = yaml.safe_load(load_profile_resource("sddc_manager_minimal.yaml"))
    shipped = ExecutionProfile.model_validate(raw)
    assert shipped == SDDC_EXECUTION_PROFILE
    assert SDDC_EXECUTION_PROFILE.auth.scheme == "session_login_token"
