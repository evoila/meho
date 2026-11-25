# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A target's own secret path outside the tenant's folder: log warning and admin list.

* Every store read of a target's own secret checks the path against the
  tenant's folder. A path outside it writes one warning per target per
  hour to the server log; the read goes on and the answer is the same.
* ``GET /api/v1/targets/checks/outside-tenant-folder`` lists the targets whose
  path is outside the folder, for admins.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared import vault_creds
from meho_backplane.connectors._shared.vault_creds import (
    OperatorTokenRequiredError,
    VaultCredentialsReadError,
    load_basic_credentials,
)
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref
from meho_backplane.connectors.vault.tenant_scope import (
    REASON_OUTSIDE_TENANT,
    REASON_PATH_COULD_MOVE,
    VaultTenantScopeError,
    enforce_tenant_scope,
    outside_tenant_folder_reason,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Tenant
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_client

_TENANT = UUID(DEFAULT_TENANT_ID)
_OTHER_TENANT = UUID("00000000-0000-0000-0000-00000000b0b0")
_TARGET_ID = UUID("00000000-0000-0000-0000-0000000000aa")
_EVENT = "target_secret_ref_outside_tenant_folder"
_CANARY = "p4ssw0rd-canary-must-not-show"


@pytest.fixture(autouse=True)
def _fresh_warning_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    monkeypatch.delenv("VAULT_OIDC_ROLE_BY_TENANT", raising=False)
    vault_creds._folder_warning_last.clear()
    get_settings.cache_clear()
    yield
    vault_creds._folder_warning_last.clear()
    get_settings.cache_clear()


@dataclass
class _Target:
    name: str = "vc-lab-01"
    host: str = "vc-lab-01.example.test"
    secret_ref: str | None = "targets/vc-lab-01"
    id: UUID = _TARGET_ID


def _operator(**kwargs: Any) -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        **kwargs,
    )


def _warnings(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event") == _EVENT]


async def _read(monkeypatch: pytest.MonkeyPatch, target: _Target, operator: Operator) -> Any:
    install_fake_client(monkeypatch, secret={"username": "u", "password": _CANARY})
    with capture_logs() as captured:
        creds = await load_basic_credentials(target, operator)
    return creds, captured


# ---------------------------------------------------------------------------
# The log warning
# ---------------------------------------------------------------------------


async def test_a_path_outside_the_folder_warns_and_the_read_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds, captured = await _read(monkeypatch, _Target(), _operator())

    assert creds == {"username": "u", "password": _CANARY}
    warnings = _warnings(captured)
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["target_id"] == "00000000-0000-0000-0000-0000000000aa"
    assert warnings[0]["target_name"] == "vc-lab-01"
    assert warnings[0]["tenant_id"] == str(_TENANT)
    assert warnings[0]["secret_ref_prefix"] == "targets/…"
    assert "vc-lab-01" not in warnings[0]["secret_ref_prefix"]
    assert _CANARY not in repr(captured)


def _made_up_part(label: str) -> str:
    return f"{label}-{uuid4().hex}"


@pytest.mark.parametrize(
    ("border", "two_parts"),
    [
        ("/", True),
        ("", False),
        ("%2F", True),
        ("\\", True),
        ("?", True),
        ("#", True),
        ("%20", True),
        ("%41", True),
    ],
    ids=[
        "slash",
        "one-part",
        "encoded-slash",
        "backslash",
        "question-mark",
        "number-sign",
        "encoded-space",
        "encoded-letter",
    ],
)
async def test_the_warning_shows_only_the_first_part_of_the_path(
    monkeypatch: pytest.MonkeyPatch, border: str, two_parts: bool
) -> None:
    first = _made_up_part("first")
    second = _made_up_part("second")
    ref = f"{first}{border}{second}" if two_parts else first
    if border.startswith("%") and border != "%2F":
        ref = f"{ref}/{_made_up_part('third')}"

    creds, captured = await _read(monkeypatch, _Target(secret_ref=ref), _operator())

    assert creds == {"username": "u", "password": _CANARY}
    warnings = _warnings(captured)
    assert len(warnings) == 1
    hidden = second if two_parts else first
    assert hidden not in repr(captured)
    if two_parts:
        assert warnings[0]["secret_ref_prefix"] == f"{first}/…"
    else:
        assert warnings[0]["secret_ref_prefix"] == "…"


async def test_a_path_inside_the_folder_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = tenant_secret_ref(_TENANT, "vc-lab-01")
    _, captured = await _read(monkeypatch, _Target(secret_ref=ref), _operator())
    assert _warnings(captured) == []


async def test_the_warning_comes_once_per_target_per_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})
    clock = [1000.0]
    monkeypatch.setattr(vault_creds.time, "monotonic", lambda: clock[0])
    target = _Target()
    with capture_logs() as captured:
        await load_basic_credentials(target, _operator())
        await load_basic_credentials(target, _operator())
        await load_basic_credentials(_Target(name="other", id=UUID(int=7)), _operator())
        clock[0] += 3601.0
        await load_basic_credentials(target, _operator())
    assert [e["target_name"] for e in _warnings(captured)] == ["vc-lab-01", "other", "vc-lab-01"]


async def test_the_check_off_install_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    creds, captured = await _read(monkeypatch, _Target(), _operator())
    assert creds["password"] == _CANARY
    assert _warnings(captured) == []


async def test_a_path_that_could_move_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    _, captured = await _read(
        monkeypatch, _Target(secret_ref=f"tenants/{_TENANT}/../x"), _operator()
    )
    assert len(_warnings(captured)) == 1


async def test_the_check_runner_is_still_refused_and_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})
    with capture_logs() as captured, pytest.raises(VaultTenantScopeError):
        await load_basic_credentials(_Target(), _operator(check_runner_dispatch=True))
    assert _warnings(captured) == []


async def test_an_error_in_the_warning_step_does_not_stop_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("path check failed")

    monkeypatch.setattr(
        "meho_backplane.connectors.vault.tenant_scope.outside_tenant_folder_reason", _boom
    )
    creds, captured = await _read(monkeypatch, _Target(), _operator())
    assert creds == {"username": "u", "password": _CANARY}
    assert _warnings(captured) == []


async def test_an_error_in_the_log_call_does_not_stop_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_ref: str) -> str:
        raise ValueError("cut failed")

    monkeypatch.setattr(vault_creds, "_folder_warning_prefix", _boom)
    creds, _ = await _read(monkeypatch, _Target(), _operator())
    assert creds == {"username": "u", "password": _CANARY}


async def test_a_read_with_no_operator_token_is_refused_and_writes_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})
    operator = _operator().model_copy(update={"raw_jwt": ""})
    with capture_logs() as captured, pytest.raises(OperatorTokenRequiredError):
        await load_basic_credentials(_Target(), operator)
    assert _warnings(captured) == []


async def test_a_path_in_the_store_api_form_is_refused_and_writes_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})
    target = _Target(secret_ref="secret/data/targets/vc-lab-01")
    with capture_logs() as captured, pytest.raises(VaultCredentialsReadError):
        await load_basic_credentials(target, _operator())
    assert _warnings(captured) == []


# ---------------------------------------------------------------------------
# The caller sees no change
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


class _DispatchTarget:
    product = "vault"
    version = "1.x"
    preferred_impl_id: str | None = None
    fingerprint = None
    host = "t.example.test"
    port = 443
    auth_model = "shared_service_account"

    def __init__(self, *, name: str, secret_ref: str) -> None:
        self.id = uuid4()
        self.name = name
        self.secret_ref = secret_ref


async def _read_credentials_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    creds = await load_basic_credentials(target, operator)
    return {"user": creds["username"], "reachable": True}


def _register_dispatch_connector() -> None:
    from meho_backplane.connectors.base import Connector
    from meho_backplane.connectors.registry import register_connector_v2

    class _Stub(Connector):
        product = "vault"
        version = "1.x"
        impl_id = "vault"

        async def fingerprint(self, target: Any, operator: Any = None) -> Any:
            raise NotImplementedError

        async def probe(self, target: Any) -> Any:
            raise NotImplementedError

        async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:
            raise NotImplementedError

    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="", impl_id="", cls=_Stub)


async def _dispatch_and_audit(
    monkeypatch: pytest.MonkeyPatch, target: _DispatchTarget
) -> tuple[Any, dict[str, Any], int]:
    install_fake_client(monkeypatch, secret={"username": "u", "password": _CANARY})
    with capture_logs() as captured:
        result = await dispatch(
            operator=_operator(),
            connector_id="vault-1.x",
            op_id="vault.folder.read",
            target=target,
            params={},
        )
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == "vault.folder.read")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    columns = {c.key: getattr(rows[0], c.key) for c in AuditLog.__table__.columns}
    text = json.dumps(columns, default=str, sort_keys=True)
    for secret_text, mark in (
        (str(target.id), "<id>"),
        (target.name, "<name>"),
        (target.secret_ref, "<path>"),
    ):
        text = text.replace(secret_text, mark)
    row = json.loads(text)
    for volatile in (
        "id",
        "occurred_at",
        "duration_ms",
        "request_id",
        "correlation_id",
        "trace_id",
    ):
        row.pop(volatile, None)
    return result, row, len(_warnings(captured))


async def test_the_caller_answer_and_the_audit_row_are_the_same_inside_and_outside(
    monkeypatch: pytest.MonkeyPatch, stub_embedding_service: AsyncMock
) -> None:
    _register_dispatch_connector()
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id="vault.folder.read",
        handler=_read_credentials_handler,
        summary="Read a target login.",
        description="Read.",
        parameter_schema={"type": "object"},
        safety_level="safe",
        when_to_use=None,
        embedding_service=stub_embedding_service,
    )
    inside = _DispatchTarget(name="inside-t", secret_ref=tenant_secret_ref(_TENANT, "inside-t"))
    inside_result, inside_row, inside_warnings = await _dispatch_and_audit(monkeypatch, inside)
    async with get_sessionmaker()() as session:
        for old in (await session.execute(select(AuditLog))).scalars().all():
            await session.delete(old)
        await session.commit()
    outside = _DispatchTarget(name="outside-t", secret_ref="targets/outside-t")
    outside_result, outside_row, outside_warnings = await _dispatch_and_audit(monkeypatch, outside)

    assert (inside_warnings, outside_warnings) == (0, 1)
    assert inside_result.status == outside_result.status == "ok"
    assert inside_result.result == outside_result.result
    assert inside_row == outside_row
    assert _EVENT not in json.dumps(outside_row)
    assert _EVENT not in repr(outside_result)
    clear_registry()
    reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# The helper uses the same rules as the refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"tenants/{_TENANT}/x",
        f"tenants/{_TENANT}",
        f"tenants/{_TENANT}-evil/x",
        f"tenants/{_OTHER_TENANT}/x",
        "targets/x",
        f"tenants/{_TENANT}/../{_OTHER_TENANT}/x",
        f"tenants/{_TENANT}/a%2fb",
        f"/tenants/{_TENANT}/x/",
        "meho/test/federation",
    ],
)
def test_the_helper_agrees_with_the_refusal(path: str) -> None:
    operator = _operator()
    reason = outside_tenant_folder_reason(_TENANT, mount="secret", path=path, read_only=True)
    try:
        enforce_tenant_scope(operator, mount="secret", path=path, read_only=True)
    except VaultTenantScopeError as exc:
        assert reason == exc.reason
    else:
        assert reason is None


def test_the_helper_reasons_and_the_off_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        outside_tenant_folder_reason(_TENANT, mount="secret", path="a/b") == REASON_OUTSIDE_TENANT
    )
    assert (
        outside_tenant_folder_reason(_TENANT, mount="secret", path="a/../b")
        == REASON_PATH_COULD_MOVE
    )
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    assert outside_tenant_folder_reason(_TENANT, mount="secret", path="a/b") is None


# ---------------------------------------------------------------------------
# The admin list
# ---------------------------------------------------------------------------

_URL = "/api/v1/targets/checks/outside-tenant-folder"


def _get(
    client: TestClient,
    key: Any,
    url: str = _URL,
    headers: dict[str, str] | None = None,
    **claims: Any,
) -> Any:
    token = mint_token(key, **claims)
    return client.get(url, headers={"Authorization": f"Bearer {token}", **(headers or {})})


def _app_with(router: Any) -> Any:
    """The app of the targets tests, with *router* first, as in ``main.py``."""
    app = _build_app()
    app.router.routes[:] = []
    app.include_router(router)
    app.include_router(targets_router)
    return app


async def _seed() -> None:
    await _insert_target(name="inside", secret_ref=tenant_secret_ref(_TENANT, "inside"))
    await _insert_target(name="hand-set", secret_ref=f"tenants/{_TENANT}/shared")
    await _insert_target(name="outside", secret_ref="targets/outside")
    await _insert_target(name="gsm", secret_ref="gsm:project/secret")
    await _insert_target(name="none", secret_ref=None)
    await _insert_target(name="other", tenant_id=_OTHER_TENANT, secret_ref="targets/other")


@pytest.mark.asyncio
async def test_the_list_scopes_by_role() -> None:
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _seed()
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            own = _get(client, key, tenant_role=TenantRole.TENANT_ADMIN.value)
            platform = _get(
                client, key, tenant_role=TenantRole.TENANT_ADMIN.value, platform_admin=True
            )
            one = _get(
                client,
                key,
                url=f"{_URL}?tenant_id={_OTHER_TENANT}",
                tenant_role=TenantRole.TENANT_ADMIN.value,
                platform_admin=True,
            )
            foreign = _get(
                client,
                key,
                url=f"{_URL}?tenant_id={_OTHER_TENANT}",
                tenant_role=TenantRole.TENANT_ADMIN.value,
            )
            operator = _get(client, key, tenant_role=TenantRole.OPERATOR.value)
    assert own.status_code == 200, own.text
    body = own.json()
    assert body["all_tenants"] is False
    assert body["check_off"] is False
    assert [(i["name"], i["secret_ref"], i["reason"]) for i in body["items"]] == [
        ("outside", "targets/outside", REASON_OUTSIDE_TENANT)
    ]
    assert platform.json()["all_tenants"] is True
    assert {(i["tenant_id"], i["name"]) for i in platform.json()["items"]} == {
        (DEFAULT_TENANT_ID, "outside"),
        (str(_OTHER_TENANT), "other"),
    }
    assert [i["name"] for i in one.json()["items"]] == ["other"]
    assert foreign.status_code == 403
    assert operator.status_code == 403


_THIRD_TENANT = UUID("00000000-0000-0000-0000-00000000c0c0")


@pytest.mark.asyncio
async def test_the_list_keeps_the_role_of_each_member_tenant() -> None:
    """A platform admin who reads only in a member tenant does not get that tenant's list."""
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _seed()
    await _insert_target(name="third", tenant_id=_THIRD_TENANT, secret_ref="targets/third")
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    member_of = {
        "meho_tenants": [
            f"{_TENANT}:lab-a:Admins",
            f"{_OTHER_TENANT}:lab-b:Readers",
        ]
    }
    claims: dict[str, Any] = {
        "tenant_role": TenantRole.TENANT_ADMIN.value,
        "platform_admin": True,
        "extra_claims": member_of,
        "headers": {"Meho-Tenant": str(_TENANT)},
    }
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            every = _get(client, key, **claims)
            read_only = _get(client, key, url=f"{_URL}?tenant_id={_OTHER_TENANT}", **claims)
            not_member = _get(client, key, url=f"{_URL}?tenant_id={_THIRD_TENANT}", **claims)
            chosen = _get(
                client,
                key,
                tenant_role=TenantRole.TENANT_ADMIN.value,
                platform_admin=True,
                extra_claims=member_of,
                headers={"Meho-Tenant": str(_OTHER_TENANT)},
            )
    assert every.status_code == 200, every.text
    assert every.json()["all_tenants"] is True
    assert {(i["tenant_id"], i["name"]) for i in every.json()["items"]} == {
        (DEFAULT_TENANT_ID, "outside"),
        (str(_THIRD_TENANT), "third"),
    }
    assert read_only.status_code == 403
    assert read_only.json()["detail"].startswith("tenant_select_with_header:")
    assert not_member.status_code == 200, not_member.text
    assert [i["name"] for i in not_member.json()["items"]] == ["third"]
    assert chosen.status_code == 403
    assert chosen.json()["detail"] == "insufficient_role"


@pytest.mark.asyncio
async def test_a_machine_login_with_the_platform_admin_claim_lists_only_its_own_tenant() -> None:
    """A service login gets only its own tenant's rows; an agent login gets 403."""
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _seed()
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    claims: dict[str, Any] = {
        "tenant_role": TenantRole.TENANT_ADMIN.value,
        "platform_admin": True,
    }
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            service = _get(client, key, principal_kind="service", **claims)
            service_other = _get(
                client,
                key,
                url=f"{_URL}?tenant_id={_OTHER_TENANT}",
                principal_kind="service",
                **claims,
            )
            agent = _get(client, key, principal_kind="agent", **claims)
    assert service.status_code == 200, service.text
    assert service.json()["all_tenants"] is False
    assert {(i["tenant_id"], i["name"]) for i in service.json()["items"]} == {
        (DEFAULT_TENANT_ID, "outside"),
    }
    assert service_other.status_code == 403
    assert agent.status_code == 403


@pytest.mark.asyncio
async def test_the_list_of_every_tenant_leaves_out_an_operator_member_tenant() -> None:
    """A member tenant where the admin is only an operator is not in the list of every tenant."""
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _seed()
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            every = _get(
                client,
                key,
                tenant_role=TenantRole.TENANT_ADMIN.value,
                platform_admin=True,
                extra_claims={
                    "meho_tenants": [
                        f"{_TENANT}:lab-a:Admins",
                        f"{_OTHER_TENANT}:lab-b:Operators",
                    ]
                },
                headers={"Meho-Tenant": str(_TENANT)},
            )
    assert every.status_code == 200, every.text
    assert every.json()["all_tenants"] is True
    assert {(i["tenant_id"], i["name"]) for i in every.json()["items"]} == {
        (DEFAULT_TENANT_ID, "outside"),
    }


async def _seed_tenant_row(tenant_id: UUID, slug: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=tenant_id, slug=slug, name=slug))
        await session.commit()


@pytest.mark.asyncio
async def test_a_token_without_a_tenant_list_keeps_the_role_of_its_own_tenant() -> None:
    """Read-only in the token's tenant, then working in another tenant: no rows of the first."""
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _seed()
    await _seed_tenant_row(_OTHER_TENANT, "lab-b")
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    claims: dict[str, Any] = {
        "tenant_role": TenantRole.READ_ONLY.value,
        "platform_admin": True,
        "headers": {"Meho-Tenant": str(_OTHER_TENANT)},
    }
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            every = _get(client, key, **claims)
            token_tenant = _get(client, key, url=f"{_URL}?tenant_id={_TENANT}", **claims)
    assert every.status_code == 200, every.text
    assert every.json()["all_tenants"] is True
    assert {(i["tenant_id"], i["name"]) for i in every.json()["items"]} == {
        (str(_OTHER_TENANT), "other"),
    }
    assert token_tenant.status_code == 403
    assert token_tenant.json()["detail"].startswith("tenant_select_with_header:")


@pytest.mark.asyncio
async def test_a_chosen_member_tenant_with_its_id_lists_only_that_tenant() -> None:
    """``meho tenants use <name>``, then ``--tenant <its id>``: only that tenant's rows."""
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _seed()
    await _insert_target(name="third", tenant_id=_THIRD_TENANT, secret_ref="targets/third")
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            chosen = _get(
                client,
                key,
                url=f"{_URL}?tenant_id={_TENANT}",
                tenant_role=TenantRole.TENANT_ADMIN.value,
                platform_admin=True,
                extra_claims={
                    "meho_tenants": [
                        f"{_TENANT}:lab-a:Admins",
                        f"{_OTHER_TENANT}:lab-b:Readers",
                    ]
                },
                headers={"Meho-Tenant": "lab-a"},
            )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["all_tenants"] is False
    assert {(i["tenant_id"], i["name"]) for i in chosen.json()["items"]} == {
        (DEFAULT_TENANT_ID, "outside"),
    }


@pytest.mark.asyncio
async def test_a_target_with_the_check_name_is_still_read_by_name() -> None:
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _insert_target(name="outside-tenant-folder", aliases=["checks"])
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            by_name = _get(
                client,
                key,
                url="/api/v1/targets/outside-tenant-folder",
                tenant_role=TenantRole.OPERATOR.value,
            )
            by_alias = _get(
                client, key, url="/api/v1/targets/checks", tenant_role=TenantRole.OPERATOR.value
            )
    assert by_name.status_code == 200, by_name.text
    assert by_name.json()["name"] == "outside-tenant-folder"
    assert by_alias.status_code == 200, by_alias.text
    assert by_alias.json()["name"] == "outside-tenant-folder"


@pytest.mark.asyncio
async def test_the_list_says_the_check_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()
    await _seed()
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            own = _get(client, key, tenant_role=TenantRole.TENANT_ADMIN.value)
            platform = _get(
                client, key, tenant_role=TenantRole.TENANT_ADMIN.value, platform_admin=True
            )
    for response in (own, platform):
        assert response.status_code == 200, response.text
        assert response.json()["check_off"] is True
        assert response.json()["message"] == "The folder check is off on this install"
        assert response.json()["items"] == []


async def _built_path_read_and_list(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """Read the secret of a target with the path MEHO builds, then ask the admin list."""
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    ref = tenant_secret_ref(_TENANT, "built")
    _, captured = await _read(monkeypatch, _Target(name="built", secret_ref=ref), _operator())
    await _insert_target(name="built", secret_ref=ref)
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            listed = _get(client, key, tenant_role=TenantRole.TENANT_ADMIN.value)
    assert listed.status_code == 200, listed.text
    return _warnings(captured), [i["name"] for i in listed.json()["items"]]


@pytest.mark.asyncio
async def test_a_built_path_outside_another_folder_warns_and_is_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "kv/{tenant_id}/")
    get_settings.cache_clear()

    warnings, names = await _built_path_read_and_list(monkeypatch)

    assert [w["target_name"] for w in warnings] == ["built"]
    assert names == ["built"]


@pytest.mark.asyncio
async def test_a_built_path_inside_the_default_folder_is_not_warned_or_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings, names = await _built_path_read_and_list(monkeypatch)

    assert warnings == []
    assert names == []


@pytest.mark.asyncio
async def test_a_deleted_target_is_not_in_the_list() -> None:
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    await _insert_target(name="live", secret_ref="targets/live")
    await _insert_target(name="gone", secret_ref="targets/gone", deleted_at=datetime.now(UTC))
    await _insert_target(
        name="gone-other",
        tenant_id=_OTHER_TENANT,
        secret_ref="targets/gone-other",
        deleted_at=datetime.now(UTC),
    )
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            tenant_admin = _get(client, key, tenant_role=TenantRole.TENANT_ADMIN.value)
            platform = _get(
                client, key, tenant_role=TenantRole.TENANT_ADMIN.value, platform_admin=True
            )
    assert tenant_admin.status_code == 200, tenant_admin.text
    assert [i["name"] for i in tenant_admin.json()["items"]] == ["live"]
    assert platform.status_code == 200, platform.text
    assert [i["name"] for i in platform.json()["items"]] == ["live"]


@pytest.mark.asyncio
async def test_a_tenant_with_its_own_store_role_is_listed_and_its_read_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.api.v1.targets_outside_tenant_folder import router

    monkeypatch.setenv("VAULT_OIDC_ROLE_BY_TENANT", f"{_TENANT}=meho-own-role")
    get_settings.cache_clear()
    await _insert_target(name="outside", secret_ref="targets/outside")
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            listed = _get(client, key, tenant_role=TenantRole.TENANT_ADMIN.value)
    assert listed.status_code == 200, listed.text
    assert listed.json()["check_off"] is False
    assert [(i["name"], i["reason"]) for i in listed.json()["items"]] == [
        ("outside", REASON_OUTSIDE_TENANT)
    ]

    install_fake_client(monkeypatch, secret={"username": "u", "password": "p"})
    with capture_logs() as captured, pytest.raises(VaultTenantScopeError):
        await load_basic_credentials(
            _Target(name="outside", secret_ref="targets/outside"), _operator()
        )
    assert _warnings(captured) == []


@pytest.mark.asyncio
async def test_an_older_route_refuses_the_own_tenant_of_the_token() -> None:
    """``GET /api/v1/sensors?tenant_filter=<token tenant>`` with another tenant chosen."""
    from meho_backplane.api.v1.sensors import router

    await _seed_tenant_row(_OTHER_TENANT, "lab-b")
    app = _app_with(router)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock:
        mock_discovery_and_jwks(mock, public_jwks(key))
        with TestClient(app) as client:
            response = _get(
                client,
                key,
                url=f"/api/v1/sensors?tenant_filter={_TENANT}",
                tenant_role=TenantRole.READ_ONLY.value,
                platform_admin=True,
                headers={"Meho-Tenant": str(_OTHER_TENANT)},
            )
    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("tenant_select_with_header:")
