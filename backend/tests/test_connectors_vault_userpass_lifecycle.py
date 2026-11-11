# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the userpass password sources and the disable / enable ops.

Covers ``vault.auth.userpass.write`` with ``password_secret_ref`` and
``generate_password``, and ``vault.auth.userpass.disable`` / ``.enable``.
The handlers run against a small in-memory fake of the hvac client. Every
test password is a made-up value, and the tests check that it appears only
in the fake KV store and the fake login, never in a result.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac.exceptions
import jsonschema
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vault import (
    ops_auth_write,
    ops_userpass_lifecycle,
    register_vault_typed_operations,
)
from meho_backplane.connectors.vault.ops_auth import VaultAuthBackendNotMountedError
from meho_backplane.connectors.vault.ops_auth_write import vault_auth_userpass_write
from meho_backplane.connectors.vault.ops_userpass_lifecycle import (
    VaultUserpassUserNotFoundError,
    vault_auth_userpass_disable,
    vault_auth_userpass_enable,
)
from meho_backplane.connectors.vault.ops_userpass_write_schemas import (
    VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.vault.tenant_scope import VaultTenantScopeError
from meho_backplane.connectors.vault.userpass_password import (
    PasswordSecretError,
    generate_password_value,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch
from meho_backplane.settings import get_settings

ACCESSOR = "auth_userpass_0001"
SENTINEL = "Test-Only-Password-9f3a"


class _Fake:
    """One object that plays the hvac client for the calls the handlers make."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.kv: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.entities: dict[str, dict[str, Any]] = {}
        self.aliases: dict[tuple[str, str], str] = {}
        self.revoked: list[str] = []
        self.lease_keys: list[str] = []
        self.mounts = {"userpass/": {"accessor": ACCESSOR}}
        self.user_writes: list[dict[str, Any]] = []
        self.kv_writes: list[dict[str, Any]] = []
        self.client = self
        self.auth = self
        self.userpass = self
        self.secrets = self
        self.identity = self
        self.kv_ns = self
        self.v2 = self
        self.sys = self

    # hvac exposes secrets.kv.v2; point .kv at self so .kv.v2 is self too.
    @property
    def kv_api(self) -> Any:
        return self

    # --- userpass
    def read_user(self, username: str, mount_point: str = "userpass") -> dict[str, Any]:
        if username.lower() not in self.users:
            raise hvac.exceptions.InvalidPath()
        return {"data": self.users[username.lower()]}

    def create_or_update_user(
        self, username: str, password: str | None = None, mount_point: str = "userpass", **kw: Any
    ) -> None:
        self.user_writes.append({"username": username, "has_password": password is not None})
        user = self.users.setdefault(username.lower(), {})
        if password is not None:
            user["_password"] = password
        if "token_policies" in kw:
            user["token_policies"] = kw["token_policies"]

    # --- kv v2
    def read_secret_version(
        self, path: str, mount_point: str = "secret", **_kw: Any
    ) -> dict[str, Any]:
        versions = self.kv.get((mount_point, path))
        if not versions:
            raise hvac.exceptions.InvalidPath()
        return {
            "data": {"data": dict(versions[-1]), "metadata": {"version": len(versions)}},
        }

    def create_or_update_secret(
        self, path: str, secret: dict[str, Any], cas: int | None = None, mount_point: str = "secret"
    ) -> None:
        versions = self.kv.setdefault((mount_point, path), [])
        if cas is not None and cas != len(versions):
            raise hvac.exceptions.InvalidRequest("check-and-set parameter did not match")
        self.kv_writes.append({"path": path, "cas": cas})
        versions.append(dict(secret))

    # --- sys
    def list_auth_methods(self) -> dict[str, Any]:
        return {"data": self.mounts}

    def revoke_prefix(self, prefix: str) -> None:
        self.revoked.append(prefix)

    def list_leases(self, prefix: str) -> dict[str, Any]:
        if not self.lease_keys:
            raise hvac.exceptions.InvalidPath()
        return {"data": {"keys": list(self.lease_keys)}}

    # --- identity
    def lookup_entity(
        self, alias_name: str | None = None, alias_mount_accessor: str | None = None, **_kw: Any
    ) -> dict[str, Any] | None:
        entity_id = self.aliases.get((alias_name or "", alias_mount_accessor or ""))
        if entity_id is None:
            return None
        return {"data": {"id": entity_id, **self.entities[entity_id]}}

    def read_entity_by_name(self, name: str) -> dict[str, Any]:
        for entity_id, entity in self.entities.items():
            if entity["name"] == name:
                return {"data": {"id": entity_id, **entity}}
        raise hvac.exceptions.InvalidPath()

    def create_or_update_entity(
        self, name: str | None, metadata: dict[str, str] | None = None, **_kw: Any
    ) -> dict[str, Any]:
        # Like Vault: a known name updates that entity; no name makes a new one.
        for entity_id, entity in self.entities.items():
            if name is not None and entity["name"] == name:
                return {"data": {"id": entity_id}}
        entity_id = f"entity-{len(self.entities) + 1}"
        self.entities[entity_id] = {
            "name": name or f"entity_{entity_id}",
            "disabled": False,
            "metadata": metadata,
        }
        return {"data": {"id": entity_id}}

    def create_or_update_entity_alias(
        self, name: str, canonical_id: str, mount_accessor: str
    ) -> None:
        self.aliases[(name, mount_accessor)] = canonical_id

    def update_entity(self, entity_id: str, disabled: bool | None = None, **_kw: Any) -> None:
        if disabled is not None:
            self.entities[entity_id]["disabled"] = disabled


class _KvShim:
    """``client.secrets.kv.v2`` -> the fake."""

    def __init__(self, fake: _Fake) -> None:
        self.v2 = fake


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Fake]:
    f = _Fake()
    f.secrets = type("S", (), {})()  # type: ignore[assignment]
    f.secrets.kv = _KvShim(f)  # type: ignore[attr-defined]
    f.secrets.identity = f  # type: ignore[attr-defined]

    @asynccontextmanager
    async def _client(_operator: Any, _target: Any) -> AsyncIterator[_Fake]:
        yield f

    monkeypatch.setattr(ops_auth_write, "vault_client_for_target", _client)
    monkeypatch.setattr(ops_userpass_lifecycle, "vault_client_for_target", _client)
    # The write handler reads the tenant prefix from the settings.
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    yield f
    get_settings.cache_clear()


def _operator(tenant: int = 0) -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=tenant),
        tenant_role=TenantRole.OPERATOR,
    )


def _write(params: dict[str, Any], operator: Operator | None = None) -> Any:
    return vault_auth_userpass_write(operator or _operator(), None, params)


def _no_secret(result: Any) -> None:
    assert SENTINEL not in json.dumps(result)


# --- password_secret_ref ----------------------------------------------------


async def test_secret_ref_reads_kv_and_sets_it(fake: _Fake) -> None:
    fake.kv[("secret", "labs/alice")] = [{"password": SENTINEL}]
    result = await _write({"username": "alice", "password_secret_ref": "labs/alice"})
    assert fake.users["alice"]["_password"] == SENTINEL
    assert result["credential_source"] == "kv_path"
    assert result["stored_mount"] == "secret"
    assert result["stored_path"] == "labs/alice"
    assert result["stored_field"] == "password"
    _no_secret(result)


async def test_secret_ref_missing_secret_is_an_error_without_a_value(fake: _Fake) -> None:
    with pytest.raises(PasswordSecretError) as err:
        await _write({"username": "alice", "password_secret_ref": "labs/none"})
    assert "labs/none" in str(err.value)
    assert "alice" not in fake.users


async def test_secret_ref_missing_field_is_an_error(fake: _Fake) -> None:
    fake.kv[("secret", "labs/alice")] = [{"other": SENTINEL}]
    with pytest.raises(PasswordSecretError) as err:
        await _write({"username": "alice", "password_secret_ref": "labs/alice"})
    assert SENTINEL not in str(err.value)


@pytest.mark.parametrize(
    "params",
    [
        {"username": "a"},
        {"username": "a", "password": "x", "password_secret_ref": "p"},
        {"username": "a", "password": "x", "generate_password": {"path": "p"}},
        {"username": "a", "password": "x", "rotate": True},
        {"username": "a", "generate_password": {"path": "../x"}},
        {"username": "a", "password_secret_ref": "a/../b"},
    ],
)
async def test_bad_credential_source_is_refused(fake: _Fake, params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await _write(params)
    assert fake.users == {}


_TENANT = UUID(int=7)


@pytest.mark.parametrize(
    "params",
    [
        {"username": "a", "password_secret_ref": "tenants/other/login"},
        {"username": "a", "generate_password": {"path": "tenants/other/login"}},
    ],
)
async def test_kv_path_of_another_tenant_is_refused_before_any_vault_call(
    fake: _Fake, params: dict[str, Any]
) -> None:
    fake.kv[("secret", "tenants/other/login")] = [{"password": SENTINEL}]
    with pytest.raises(VaultTenantScopeError):
        await _write(params, _operator(7))
    assert fake.users == {} and fake.user_writes == [] and fake.kv_writes == []
    assert fake.kv[("secret", "tenants/other/login")] == [{"password": SENTINEL}]


@pytest.mark.parametrize(
    "params",
    [
        {"username": "a", "password_secret_ref": f"tenants/{_TENANT}/login"},
        {"username": "a", "generate_password": {"path": f"tenants/{_TENANT}/login"}},
    ],
)
async def test_kv_path_of_the_own_tenant_is_allowed(fake: _Fake, params: dict[str, Any]) -> None:
    fake.kv[("secret", f"tenants/{_TENANT}/login")] = [{"password": SENTINEL}]
    result = await _write(params, _operator(7))
    assert result["written"] is True
    assert fake.users["a"]["_password"] == SENTINEL


def test_schema_needs_exactly_one_credential_source() -> None:
    schema = VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA
    jsonschema.validate({"username": "a", "password_secret_ref": "p"}, schema)
    jsonschema.validate({"username": "a", "generate_password": {"path": "p"}}, schema)
    for bad in (
        {"username": "a"},
        {"username": "a", "password": "x", "password_secret_ref": "p"},
        {"username": "a", "generate_password": {"path": "p", "length": 4}},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


# --- generate_password ------------------------------------------------------


async def test_generate_stores_in_kv_first_then_sets_the_login(fake: _Fake) -> None:
    result = await _write(
        {"username": "alice", "generate_password": {"path": "labs/alice"}, "token_policies": ["p1"]}
    )
    stored = fake.kv[("secret", "labs/alice")][-1]["password"]
    assert fake.users["alice"]["_password"] == stored
    assert len(stored) == 32
    assert fake.kv_writes[0]["cas"] == 0
    assert result["credential_generated"] is True
    assert result["written"] is True
    assert result["stored_path"] == "labs/alice"
    _no_secret(result)
    assert stored not in json.dumps(result)


async def test_generate_twice_changes_nothing(fake: _Fake) -> None:
    params = {"username": "alice", "generate_password": {"path": "labs/alice"}}
    await _write(params)
    before = (list(fake.kv[("secret", "labs/alice")]), list(fake.user_writes))
    result = await _write(params)
    assert result["written"] is False
    assert result["credential_generated"] is False
    assert (fake.kv[("secret", "labs/alice")], fake.user_writes) == before


async def test_generate_rotate_makes_a_new_password(fake: _Fake) -> None:
    params = {"username": "alice", "generate_password": {"path": "labs/alice"}}
    await _write(params)
    first = fake.kv[("secret", "labs/alice")][-1]["password"]
    result = await _write({**params, "rotate": True})
    second = fake.kv[("secret", "labs/alice")][-1]["password"]
    assert first != second
    assert fake.users["alice"]["_password"] == second
    assert result["credential_generated"] is True
    assert fake.kv_writes[-1]["cas"] == 1


async def test_generate_keeps_other_fields_and_uses_a_stored_password_for_a_new_login(
    fake: _Fake,
) -> None:
    fake.kv[("secret", "labs/alice")] = [{"password": SENTINEL, "note": "keep"}]
    result = await _write({"username": "alice", "generate_password": {"path": "labs/alice"}})
    assert fake.users["alice"]["_password"] == SENTINEL
    assert result["credential_generated"] is False
    assert len(fake.kv[("secret", "labs/alice")]) == 1
    _no_secret(result)


async def test_generate_adds_the_field_without_losing_other_fields(fake: _Fake) -> None:
    fake.kv[("secret", "labs/alice")] = [{"note": "keep"}]
    await _write({"username": "alice", "generate_password": {"path": "labs/alice", "field": "pw"}})
    last = fake.kv[("secret", "labs/alice")][-1]
    assert last["note"] == "keep"
    assert "pw" in last


async def test_generate_repeat_applies_only_a_policy_change(fake: _Fake) -> None:
    params: dict[str, Any] = {"username": "alice", "generate_password": {"path": "p"}}
    await _write({**params, "token_policies": ["a"]})
    result = await _write({**params, "token_policies": ["b"]})
    assert result["written"] is True
    assert result["credential_generated"] is False
    assert fake.user_writes[-1]["has_password"] is False
    assert fake.users["alice"]["token_policies"] == ["b"]


async def test_generate_with_unmounted_backend_is_reclassified(fake: _Fake) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise hvac.exceptions.InvalidPath()

    fake.create_or_update_user = boom  # type: ignore[method-assign]
    with pytest.raises(VaultAuthBackendNotMountedError):
        await _write({"username": "alice", "generate_password": {"path": "p"}})


def test_generated_passwords_are_random_and_mixed() -> None:
    values = {generate_password_value(20) for _ in range(20)}
    assert len(values) == 20
    for v in values:
        assert any(c.islower() for c in v) and any(c.isupper() for c in v)
        assert any(c.isdigit() for c in v)


# --- disable / enable -------------------------------------------------------


def _call(handler: Any, params: dict[str, Any]) -> Any:
    return handler(None, None, params)


async def test_disable_creates_entity_and_alias_then_disables(fake: _Fake) -> None:
    fake.users["alice"] = {}
    result = await _call(vault_auth_userpass_disable, {"username": "Alice"})
    assert result["disabled"] is True and result["changed"] is True
    assert result["entity_created"] is True
    entity = fake.entities[result["entity_id"]]
    assert entity["disabled"] is True
    assert fake.aliases[("alice", ACCESSOR)] == result["entity_id"]
    assert fake.revoked == []


async def test_disable_twice_changes_nothing(fake: _Fake) -> None:
    fake.users["alice"] = {}
    await _call(vault_auth_userpass_disable, {"username": "alice"})
    result = await _call(vault_auth_userpass_disable, {"username": "alice"})
    assert result["changed"] is False
    assert len(fake.entities) == 1


async def test_enable_turns_it_back_on_and_a_repeat_changes_nothing(fake: _Fake) -> None:
    fake.users["alice"] = {}
    await _call(vault_auth_userpass_disable, {"username": "alice"})
    result = await _call(vault_auth_userpass_enable, {"username": "alice"})
    assert result["disabled"] is False and result["changed"] is True
    again = await _call(vault_auth_userpass_enable, {"username": "alice"})
    assert again["changed"] is False
    assert "revoke_requested" not in again


async def test_enable_without_an_entity_creates_nothing(fake: _Fake) -> None:
    fake.users["alice"] = {}
    result = await _call(vault_auth_userpass_enable, {"username": "alice"})
    assert result == {
        "username": "alice",
        "mount": "userpass",
        "disabled": False,
        "changed": False,
        "entity_id": None,
        "entity_created": False,
    }
    assert fake.entities == {}


async def test_disable_with_revoke_tokens_revokes_by_login_path(fake: _Fake) -> None:
    fake.users["alice"] = {}
    result = await _call(vault_auth_userpass_disable, {"username": "Alice", "revoke_tokens": True})
    assert result["revoke_requested"] is True
    assert fake.revoked == ["auth/userpass/login/Alice/", "auth/userpass/login/alice/"]


async def test_disable_revokes_tokens_also_on_a_repeat(fake: _Fake) -> None:
    fake.users["alice"] = {}
    await _call(vault_auth_userpass_disable, {"username": "alice"})
    await _call(vault_auth_userpass_disable, {"username": "alice", "revoke_tokens": True})
    assert fake.revoked == ["auth/userpass/login/alice/"]


async def test_disable_revokes_every_spelling_that_holds_a_token(fake: _Fake) -> None:
    fake.users["alice"] = {}
    fake.lease_keys = ["ALICE/", "Alice/", "alice/", "alicex/", "bob/"]
    await _call(vault_auth_userpass_disable, {"username": "alice", "revoke_tokens": True})
    assert sorted(fake.revoked) == [
        "auth/userpass/login/ALICE/",
        "auth/userpass/login/Alice/",
        "auth/userpass/login/alice/",
    ]


async def test_disable_never_reuses_an_entity_found_by_name(fake: _Fake) -> None:
    fake.users["alice"] = {}
    fake.entities["unrelated"] = {"name": "userpass-alice", "disabled": False}
    result = await _call(vault_auth_userpass_disable, {"username": "alice"})
    assert result["entity_id"] != "unrelated"
    assert fake.entities["unrelated"]["disabled"] is False
    assert fake.aliases[("alice", ACCESSOR)] == result["entity_id"]


async def test_mounts_with_colliding_names_get_their_own_entities(fake: _Fake) -> None:
    fake.mounts = {
        "team/userpass/": {"accessor": "auth_userpass_0002"},
        "team-userpass/": {"accessor": "auth_userpass_0003"},
    }
    fake.users["alice"] = {}
    one = {"username": "alice", "mount": "team/userpass"}
    two = {"username": "alice", "mount": "team-userpass"}
    off = await _call(vault_auth_userpass_disable, one)
    other = await _call(vault_auth_userpass_disable, two)
    assert off["entity_id"] != other["entity_id"]
    await _call(vault_auth_userpass_enable, two)
    assert fake.entities[off["entity_id"]]["disabled"] is True
    assert fake.entities[other["entity_id"]]["disabled"] is False


async def test_unknown_login_is_an_error_and_creates_nothing(fake: _Fake) -> None:
    with pytest.raises(VaultUserpassUserNotFoundError):
        await _call(vault_auth_userpass_disable, {"username": "nobody"})
    assert fake.entities == {}


async def test_unmounted_backend_is_reported(fake: _Fake) -> None:
    fake.mounts = {}
    with pytest.raises(VaultAuthBackendNotMountedError):
        await _call(vault_auth_userpass_disable, {"username": "alice"})


@pytest.mark.parametrize(
    "params",
    [
        {"username": "../x"},
        {"username": ".."},
        {"username": "a/b"},
        {"username": "alice", "mount": "../sys"},
        {"username": "alice", "mount": "a//b"},
    ],
)
async def test_path_changing_values_are_refused(fake: _Fake, params: dict[str, Any]) -> None:
    fake.users["alice"] = {}
    with pytest.raises(ValueError):
        await _call(vault_auth_userpass_disable, {**params, "revoke_tokens": True})
    assert fake.revoked == [] and fake.entities == {}


async def test_non_default_mount_uses_its_own_accessor(fake: _Fake) -> None:
    fake.mounts = {"team/userpass/": {"accessor": "auth_userpass_0002"}}
    fake.users["alice"] = {}
    result = await _call(
        vault_auth_userpass_disable, {"username": "alice", "mount": "team/userpass"}
    )
    assert fake.aliases[("alice", "auth_userpass_0002")] == result["entity_id"]
    assert fake.entities[result["entity_id"]]["metadata"] == {
        "userpass_mount": "team/userpass",
        "userpass_username": "alice",
    }


# --- through the dispatcher (redaction, audit, registration) ------------------


@pytest.fixture
async def registered(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_vault_typed_operations(embedding_service=service)
    yield
    get_settings.cache_clear()


async def _dispatch(op_id: str, params: dict[str, Any]) -> Any:
    operator = Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )
    return await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=True,
    )


async def test_generated_password_reaches_only_kv_and_the_login(
    fake: _Fake, registered: None, caplog: pytest.LogCaptureFixture
) -> None:
    result = await _dispatch(
        "vault.auth.userpass.write",
        {"username": "alice", "generate_password": {"path": "labs/alice"}},
    )
    assert result.status == "ok", result.error
    stored = fake.kv[("secret", "labs/alice")][-1]["password"]
    assert fake.users["alice"]["_password"] == stored
    assert result.result["stored_mount"] == "secret"
    assert result.result["stored_path"] == "labs/alice"
    assert result.result["stored_field"] == "password"
    assert result.result["credential_generated"] is True
    assert "REDACTED" not in json.dumps(result.result)
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert rows
    for row in rows:
        assert stored not in json.dumps(row.payload, default=str)
    assert stored not in json.dumps(result.result)
    assert stored not in caplog.text


async def test_disable_and_enable_dispatch_with_a_readable_result(
    fake: _Fake, registered: None
) -> None:
    fake.users["alice"] = {}
    off = await _dispatch(
        "vault.auth.userpass.disable", {"username": "alice", "revoke_tokens": True}
    )
    assert off.status == "ok", off.error
    assert off.result["disabled"] is True
    assert off.result["revoke_requested"] is True
    assert "REDACTED" not in json.dumps(off.result)
    on = await _dispatch("vault.auth.userpass.enable", {"username": "alice"})
    assert on.status == "ok", on.error
    assert on.result["disabled"] is False


@pytest.mark.parametrize("op_id", ["vault.auth.userpass.disable", "vault.auth.userpass.enable"])
async def test_switch_ops_are_dangerous_and_need_approval(registered: None, op_id: str) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True


# --- approval preview ---------------------------------------------------------


async def test_preview_names_the_kv_path_for_a_generated_password() -> None:
    from tests.test_credential_write_previews import _preview

    _, preview = await _preview(
        "vault.auth.userpass.write",
        {"username": "alice", "generate_password": {"path": "labs/alice"}, "rotate": True},
    )
    assert preview["credential_source"] == "generated"
    assert preview["stored_mount"] == "secret"
    assert preview["stored_path"] == "labs/alice"
    assert preview["stored_field"] == "password"
    assert preview["rotate"] is True


async def test_preview_names_the_kv_path_for_a_password_reference() -> None:
    from tests.test_credential_write_previews import _preview

    _, preview = await _preview(
        "vault.auth.userpass.write",
        {"username": "alice", "password_secret_ref": "labs/alice", "password_secret_mount": "kv"},
    )
    assert preview["credential_source"] == "kv_path"
    assert preview["stored_mount"] == "kv"
