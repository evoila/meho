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

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
import respx
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from meho_backplane.api.v1.targets import router as targets_router
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared import vault_creds
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials
from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref
from meho_backplane.connectors.vault.tenant_scope import (
    REASON_OUTSIDE_TENANT,
    REASON_PATH_COULD_MOVE,
    VaultTenantScopeError,
    enforce_tenant_scope,
    outside_tenant_folder_reason,
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
    assert warnings[0]["secret_ref_prefix"] == "targets"
    assert "vc-lab-01" not in warnings[0]["secret_ref_prefix"]
    assert _CANARY not in repr(captured)


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
