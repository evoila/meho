# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for picking the current tenant of a request (#5239).

Covers :mod:`meho_backplane.auth.current_tenant`: every cell of the rules
table, one role group per tenant, the platform admin across tenants,
machine logins, and the things that read ``Operator.tenant_id`` (the role
check, a result link, the Vault role, the Vault path guard). The MCP side
is in ``test_mcp_tenant_gate.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import respx
import structlog
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.api.v1 import health as health_module
from meho_backplane.auth import current_tenant
from meho_backplane.auth.current_tenant import (
    bind_tenant_role_source,
    select_current_tenant,
    tenant_role_source_payload,
)
from meho_backplane.auth.jwt import _operator_from_claims, verify_jwt
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.auth.vault import _resolve_login_role
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.connectors.vault.tenant_scope import (
    VaultTenantScopeError,
    enforce_tenant_scope,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    _check_self_approval,
)
from meho_backplane.settings import get_settings
from tests.conftest import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_TENANT_C = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

_ENTRY_A_READERS = f"{_TENANT_A}:lab-a:Readers"
_ENTRY_B_OPERATORS = f"{_TENANT_B}:lab-b:Operators"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _claims(**extra: Any) -> dict[str, Any]:
    """A person in tenant A with the ``operator`` role (today's flat claims)."""
    claims: dict[str, Any] = {
        "sub": "person-1",
        "tenant_id": str(_TENANT_A),
        "tenant_role": "operator",
    }
    claims.update(extra)
    return claims


def _operator(**extra: Any) -> Operator:
    return _operator_from_claims(_claims(**extra), "raw.jwt.token", get_settings())


def _detail(exc: pytest.ExceptionInfo[HTTPException]) -> str:
    return str(exc.value.detail)


@pytest.fixture
def no_tenant_lookup(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record platform admin lookups; every lookup finds nothing."""
    calls: list[str] = []

    async def _lookup(wanted: str) -> uuid.UUID | None:
        calls.append(wanted)
        return None

    monkeypatch.setattr(current_tenant, "_find_existing_tenant", _lookup)
    return calls


async def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=tenant_id, slug=slug, name=slug))


async def _tenant_count() -> int:
    async with get_sessionmaker()() as session:
        return int((await session.execute(select(func.count()).select_from(Tenant))).scalar_one())


# ---------------------------------------------------------------------------
# The rules table: no list (today's tokens)
# ---------------------------------------------------------------------------


async def test_no_list_no_header_keeps_the_token_tenant() -> None:
    operator = _operator()
    assert await select_current_tenant(operator, None) == operator


async def test_no_list_header_equal_to_the_token_tenant_is_allowed() -> None:
    operator = _operator()
    assert await select_current_tenant(operator, str(_TENANT_A)) == operator
    assert await select_current_tenant(operator, f"  {str(_TENANT_A).upper()} ") == operator


async def test_no_list_header_for_another_tenant_is_refused(no_tenant_lookup: list[str]) -> None:
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(_operator(), str(_TENANT_B))
    assert exc.value.status_code == 403
    assert _detail(exc).startswith("tenant_not_member:")
    assert no_tenant_lookup == []


async def test_no_list_header_with_a_name_is_refused() -> None:
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(_operator(), "lab-a")
    assert exc.value.status_code == 403


async def test_no_list_gives_the_same_operator_as_today() -> None:
    """Every field of a token without a list is unchanged (#5239 AC)."""
    expected = Operator(
        sub="person-1",
        raw_jwt="raw.jwt.token",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
        approver=True,
    )
    operator = _operator(approver=True)
    assert operator == expected
    assert await select_current_tenant(operator, None) == expected
    assert await select_current_tenant(operator, str(_TENANT_A)) == expected


# ---------------------------------------------------------------------------
# The rules table: one tenant
# ---------------------------------------------------------------------------


async def test_one_tenant_no_header_uses_that_tenant() -> None:
    operator = await select_current_tenant(_operator(meho_tenants=[_ENTRY_B_OPERATORS]), None)
    assert operator.tenant_id == _TENANT_B
    assert operator.tenant_role is TenantRole.OPERATOR


@pytest.mark.parametrize("header", ["lab-b", "LAB-B", str(_TENANT_B)])
async def test_one_tenant_header_for_that_tenant(header: str) -> None:
    operator = await select_current_tenant(_operator(meho_tenants=[_ENTRY_B_OPERATORS]), header)
    assert operator.tenant_id == _TENANT_B


async def test_one_tenant_header_for_another_tenant_is_refused(
    no_tenant_lookup: list[str],
) -> None:
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(_operator(meho_tenants=[_ENTRY_B_OPERATORS]), "lab-a")
    assert exc.value.status_code == 403
    assert "Your tenants: lab-b." in _detail(exc)
    assert no_tenant_lookup == []


async def test_one_matching_entry_gives_the_same_operator_as_no_list() -> None:
    """A list with one entry that matches the flat claims changes nothing but ``tenants``."""
    without_list = await select_current_tenant(_operator(), None)
    with_list = await select_current_tenant(
        _operator(meho_tenants=[f"{_TENANT_A}:lab-a:Operators"]), None
    )
    assert with_list.model_dump(exclude={"tenants"}) == without_list.model_dump(exclude={"tenants"})


async def test_list_ignores_the_flat_claims() -> None:
    """Flat ``tenant_role`` / ``approver`` never apply when the token has a list."""
    operator = await select_current_tenant(
        _operator(tenant_role="tenant_admin", approver=True, meho_tenants=[_ENTRY_A_READERS]),
        None,
    )
    assert operator.tenant_role is TenantRole.READ_ONLY
    assert operator.approver is False


async def test_list_without_flat_claims_is_accepted() -> None:
    """A person only in extra-tenant groups has no flat tenant claims."""
    claims = {"sub": "person-1", "meho_tenants": [_ENTRY_B_OPERATORS]}
    operator = _operator_from_claims(claims, "raw.jwt.token", get_settings())
    assert operator.tenant_id == _TENANT_B


# ---------------------------------------------------------------------------
# The rules table: several tenants
# ---------------------------------------------------------------------------


async def test_several_tenants_no_header_is_tenant_not_chosen() -> None:
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(operator, None)
    assert exc.value.status_code == 400
    detail = _detail(exc)
    assert detail.startswith("tenant_not_chosen:")
    assert "lab-a, lab-b" in detail
    assert "meho tenants use <name>" in detail


async def test_several_tenants_blank_header_is_tenant_not_chosen() -> None:
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(operator, "   ")
    assert exc.value.status_code == 400


async def test_several_tenants_allow_unchosen_marks_the_operator() -> None:
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    unchosen = await select_current_tenant(operator, None, allow_unchosen=True)
    assert unchosen.tenant_chosen is False
    chosen = await select_current_tenant(operator, "lab-b", allow_unchosen=True)
    assert chosen.tenant_chosen is True


@pytest.mark.parametrize(
    ("header", "tenant", "role", "approver"),
    [
        ("lab-a", _TENANT_A, TenantRole.READ_ONLY, False),
        (str(_TENANT_B), _TENANT_B, TenantRole.OPERATOR, False),
        ("team3", _TENANT_C, TenantRole.READ_ONLY, True),
    ],
)
async def test_several_tenants_header_for_one_of_them(
    header: str, tenant: uuid.UUID, role: TenantRole, approver: bool
) -> None:
    operator = _operator(
        meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS, f"{_TENANT_C}:team3:Approvers"]
    )
    picked = await select_current_tenant(operator, header)
    assert (picked.tenant_id, picked.tenant_role, picked.approver) == (tenant, role, approver)
    assert picked.tenants == operator.tenants
    assert picked.role_from_platform_admin is False


async def test_several_tenants_header_for_another_tenant_is_refused(
    no_tenant_lookup: list[str],
) -> None:
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(operator, str(_TENANT_C))
    assert exc.value.status_code == 403
    assert _detail(exc).startswith("tenant_not_member:")


async def test_overlong_header_is_refused_without_a_lookup(no_tenant_lookup: list[str]) -> None:
    operator = _operator(platform_admin=True, meho_tenants=[_ENTRY_A_READERS])
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(operator, "x" * 500)
    assert exc.value.status_code == 403
    assert no_tenant_lookup == []


# ---------------------------------------------------------------------------
# One role group per tenant
# ---------------------------------------------------------------------------


def test_two_entries_for_one_tenant_are_refused() -> None:
    with pytest.raises(HTTPException) as exc:
        _operator(meho_tenants=[_ENTRY_A_READERS, f"{_TENANT_A}:lab-a:Admins", _ENTRY_B_OPERATORS])
    assert exc.value.status_code == 401
    detail = _detail(exc)
    assert detail.startswith("multiple_role_groups:")
    assert "lab-a:Readers" in detail
    assert "lab-a:Admins" in detail
    assert "lab-b" not in detail


def test_entries_for_two_tenants_are_accepted() -> None:
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    assert len(operator.tenants) == 2


def test_list_skips_the_old_groups_check() -> None:
    """With a list, being in role groups of two tenants is fine."""
    operator = _operator(
        groups=["MEHO-Readers", "MEHO-Operators"],
        meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS],
    )
    assert len(operator.tenants) == 2


def test_token_without_list_keeps_the_old_groups_check() -> None:
    with pytest.raises(HTTPException) as exc:
        _operator(groups=["MEHO-Readers", "MEHO-Operators"])
    assert exc.value.status_code == 401
    assert _detail(exc).startswith("multiple_role_groups:")


# ---------------------------------------------------------------------------
# The role follows the tenant
# ---------------------------------------------------------------------------


async def test_role_follows_the_tenant() -> None:
    """``read_only`` in A cannot run an operation; ``operator`` in B can."""
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    check = require_role(TenantRole.OPERATOR)
    in_a = await select_current_tenant(operator, "lab-a")
    with pytest.raises(HTTPException) as exc:
        check(in_a)
    assert exc.value.status_code == 403
    assert exc.value.detail == "insufficient_role"
    in_b = await select_current_tenant(operator, "lab-b")
    assert check(in_b) is in_b


# ---------------------------------------------------------------------------
# Platform admin
# ---------------------------------------------------------------------------


async def test_platform_admin_works_as_tenant_admin_in_any_existing_tenant() -> None:
    await _seed_tenant(_TENANT_C, "team3")
    operator = _operator(platform_admin=True, approver=True, meho_tenants=[_ENTRY_A_READERS])
    for header in ("team3", str(_TENANT_C)):
        picked = await select_current_tenant(operator, header)
        assert picked.tenant_id == _TENANT_C
        assert picked.tenant_role is TenantRole.TENANT_ADMIN
        assert picked.approver is False
        assert picked.role_from_platform_admin is True


async def test_platform_admin_without_list_can_name_another_tenant() -> None:
    await _seed_tenant(_TENANT_B, "lab-b")
    picked = await select_current_tenant(_operator(platform_admin=True), str(_TENANT_B))
    assert picked.tenant_id == _TENANT_B
    assert picked.tenant_role is TenantRole.TENANT_ADMIN


async def test_platform_admin_without_list_keeps_own_role_when_naming_own_tenant() -> None:
    """Naming the own tenant by name must not raise the role to tenant_admin."""
    await _seed_tenant(_TENANT_A, "lab-a")
    operator = _operator(platform_admin=True, tenant_role="read_only")
    picked = await select_current_tenant(operator, "lab-a")
    assert picked == operator
    assert picked.tenant_role is TenantRole.READ_ONLY
    assert picked.role_from_platform_admin is False


async def test_platform_admin_keeps_group_role_when_tenant_row_has_another_name() -> None:
    """The token names the tenant "lab-a"; the tenant row is "team-a"."""
    await _seed_tenant(_TENANT_A, "team-a")
    operator = _operator(platform_admin=True, meho_tenants=[_ENTRY_A_READERS])
    picked = await select_current_tenant(operator, "team-a")
    assert picked.tenant_id == _TENANT_A
    assert picked.tenant_role is TenantRole.READ_ONLY
    assert picked.role_from_platform_admin is False


async def test_platform_admin_with_a_role_group_there_gets_that_role() -> None:
    await _seed_tenant(_TENANT_A, "lab-a")
    operator = _operator(platform_admin=True, meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    picked = await select_current_tenant(operator, "lab-a")
    assert picked.tenant_role is TenantRole.READ_ONLY
    assert picked.role_from_platform_admin is False


async def test_platform_admin_unknown_tenant_is_refused_and_makes_no_row() -> None:
    operator = _operator(platform_admin=True, meho_tenants=[_ENTRY_A_READERS])
    before = await _tenant_count()
    for header in ("typo-tenant", str(uuid.uuid4())):
        with pytest.raises(HTTPException) as exc:
            await select_current_tenant(operator, header)
        assert exc.value.status_code == 403
        assert _detail(exc).startswith("tenant_not_found:")
    assert await _tenant_count() == before


async def test_platform_admin_cannot_approve_own_change() -> None:
    await _seed_tenant(_TENANT_C, "team3")
    picked = await select_current_tenant(
        _operator(platform_admin=True, meho_tenants=[_ENTRY_A_READERS]), "team3"
    )
    request = SimpleNamespace(id=uuid.uuid4(), principal_sub=picked.sub, proposed_effect={})
    with pytest.raises(SelfApprovalForbiddenError):
        _check_self_approval(picked, request)  # type: ignore[arg-type]


async def test_platform_admin_role_is_noted_on_audit_rows() -> None:
    await _seed_tenant(_TENANT_C, "team3")
    picked = await select_current_tenant(
        _operator(platform_admin=True, meho_tenants=[_ENTRY_A_READERS]), "team3"
    )
    structlog.contextvars.clear_contextvars()
    bind_tenant_role_source(picked)
    assert structlog.contextvars.get_contextvars()["audit_tenant_role_source"] == "platform_admin"
    structlog.contextvars.clear_contextvars()
    bind_tenant_role_source(await select_current_tenant(picked, "lab-a"))
    bind_tenant_role_source(_operator())
    assert "audit_tenant_role_source" not in structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()


async def test_platform_admin_role_payload_for_writers_with_their_own_payload() -> None:
    """Audit writers that do not read the contextvars get the same marker."""
    await _seed_tenant(_TENANT_C, "team3")
    picked = await select_current_tenant(
        _operator(platform_admin=True, meho_tenants=[_ENTRY_A_READERS]), "team3"
    )
    assert tenant_role_source_payload(picked) == {"tenant_role_source": "platform_admin"}
    assert tenant_role_source_payload(await select_current_tenant(picked, "lab-a")) == {}
    assert tenant_role_source_payload(_operator()) == {}


# ---------------------------------------------------------------------------
# Machine logins stay in their tenant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        (PrincipalKind.AGENT, {}),
        (PrincipalKind.SERVICE, {}),
        (PrincipalKind.RUNNER, {"runner_id": str(uuid.uuid4())}),
    ],
)
async def test_machine_login_header_for_another_tenant_is_refused(
    kind: PrincipalKind, extra: dict[str, Any], no_tenant_lookup: list[str]
) -> None:
    await _seed_tenant(_TENANT_B, "lab-b")
    operator = _operator(
        principal_kind=kind.value,
        platform_admin=True,
        meho_tenants=[_ENTRY_B_OPERATORS],
        **extra,
    )
    with pytest.raises(HTTPException) as exc:
        await select_current_tenant(operator, str(_TENANT_B))
    assert exc.value.status_code == 403
    assert _detail(exc).startswith("tenant_not_member:")
    assert no_tenant_lookup == []
    assert await select_current_tenant(operator, str(_TENANT_A)) == operator


# ---------------------------------------------------------------------------
# Things that read Operator.tenant_id use the current tenant
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        self.store[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.store.get(name)


async def test_result_link_from_tenant_a_cannot_be_read_in_tenant_b() -> None:
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    in_a = await select_current_tenant(operator, "lab-a")
    in_b = await select_current_tenant(operator, "lab-b")
    store = ResultHandleStore(_FakeRedis())  # type: ignore[arg-type]
    handle = uuid.uuid4()
    await store.spill(
        tenant_id=in_a.tenant_id,
        operator_sub=in_a.sub,
        handle_id=handle,
        op_id=None,
        rows=[{"i": i} for i in range(60)],
        total_rows=60,
        ttl_seconds=3600,
        max_rows=10000,
    )
    read_a = await store.fetch_window(
        tenant_id=in_a.tenant_id, operator_sub=in_a.sub, handle_id=handle, offset=0, limit=5
    )
    assert read_a is not None
    read_b = await store.fetch_window(
        tenant_id=in_b.tenant_id, operator_sub=in_b.sub, handle_id=handle, offset=0, limit=5
    )
    assert read_b is None


async def test_vault_login_role_follows_the_current_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_OIDC_ROLE_BY_TENANT", f"{_TENANT_A}=role-a,{_TENANT_B}=role-b")
    get_settings.cache_clear()
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    settings = get_settings()
    in_a = await select_current_tenant(operator, "lab-a")
    in_b = await select_current_tenant(operator, "lab-b")
    assert _resolve_login_role(in_a, settings, None) == "role-a"
    assert _resolve_login_role(in_b, settings, None) == "role-b"


async def test_vault_path_guard_follows_the_current_tenant() -> None:
    operator = _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    in_b = await select_current_tenant(operator, "lab-b")
    enforce_tenant_scope(in_b, mount="secret", path=f"tenants/{_TENANT_B}/db", read_only=True)
    with pytest.raises(VaultTenantScopeError):
        enforce_tenant_scope(in_b, mount="secret", path=f"tenants/{_TENANT_A}/db", read_only=True)


# ---------------------------------------------------------------------------
# End to end: the Meho-Tenant header on REST
# ---------------------------------------------------------------------------


def _mint(key: Any, **extra: Any) -> str:
    return mint_token(key, sub="person-1", extra_claims=extra)


def test_rest_header_picks_the_tenant_and_refuses_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header works on REST, and a refused header never seeds a tenant row."""
    from meho_backplane import middleware

    seeded: list[uuid.UUID] = []

    async def _record(tenant_id: uuid.UUID, _session: Any) -> None:
        seeded.append(tenant_id)

    monkeypatch.setattr(middleware, "ensure_tenant", _record)
    key = make_rsa_keypair("kid-current-tenant")
    token = _mint(key, meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(
        operator: Operator = Depends(middleware.verify_jwt_and_bind),
    ) -> dict[str, str]:
        return {"tenant_id": str(operator.tenant_id), "role": operator.tenant_role.value}

    auth = {"Authorization": f"Bearer {token}"}
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        with TestClient(app) as client:
            picked = client.get("/whoami", headers={**auth, "Meho-Tenant": "lab-b"})
            no_header = client.get("/whoami", headers=auth)
            other = client.get("/whoami", headers={**auth, "Meho-Tenant": str(_TENANT_C)})
    assert picked.status_code == 200, picked.text
    assert picked.json() == {"tenant_id": str(_TENANT_B), "role": "operator"}
    assert no_header.status_code == 400
    assert no_header.json()["detail"].startswith("tenant_not_chosen:")
    assert other.status_code == 403
    assert seeded == [_TENANT_B]


async def test_verify_jwt_called_directly_ignores_the_header_default() -> None:
    """A plain call (not through FastAPI) still works: no header means none."""
    key = make_rsa_keypair("kid-direct")
    token = _mint(key)
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        operator = await verify_jwt(f"Bearer {token}")
    assert operator.tenant_id == uuid.UUID("00000000-0000-0000-0000-00000000a0a0")


# ---------------------------------------------------------------------------
# meho status without a chosen tenant
# ---------------------------------------------------------------------------


def test_status_without_a_chosen_tenant_lists_tenants_and_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane import middleware

    seeded: list[uuid.UUID] = []

    async def _record(tenant_id: uuid.UUID, _session: Any) -> None:
        seeded.append(tenant_id)

    monkeypatch.setattr(middleware, "ensure_tenant", _record)
    key = make_rsa_keypair("kid-status")
    token = _mint(key, meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS])
    app = FastAPI()
    app.include_router(health_module.router)
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        with TestClient(app) as client:
            resp = client.get("/api/v1/health", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operator"]["tenant_id"] is None
    assert body["operator"]["tenant_role"] is None
    assert body["operator"]["tenants"] == [
        {
            "tenant_id": str(_TENANT_A),
            "name": "lab-a",
            "tenant_role": "read_only",
            "current": False,
        },
        {"tenant_id": str(_TENANT_B), "name": "lab-b", "tenant_role": "operator", "current": False},
    ]
    assert body["vault"] == {"reachable": False, "read_ok": False, "detail": "tenant_not_chosen"}
    assert body["pairings"] == []
    assert seeded == []


async def test_status_with_a_chosen_tenant_marks_the_current_one() -> None:
    operator = await select_current_tenant(
        _operator(meho_tenants=[_ENTRY_A_READERS, _ENTRY_B_OPERATORS]), "lab-b", allow_unchosen=True
    )
    identity = health_module._operator_identity(operator)
    assert identity.tenant_id == _TENANT_B
    assert [(t.name, t.current) for t in identity.tenants] == [("lab-a", False), ("lab-b", True)]
