# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.kv.generate``: a random password that meets rules, stored in KV, never shown.

These tests prove:

* every made value meets the rules, over many runs (length, each character
  class, the allowed special characters, the repeat limit);
* rules that cannot be met are refused with a message that holds no value;
* the value is in KV and nowhere else: not in the result, an error or the
  approval preview;
* other fields at the path stay, a second run changes nothing, and
  ``rotate`` writes a new version;
* a lost race, a soft-deleted secret and the tenant check fail with an error.

The Vault client is a small in-memory fake. All values are made up.
"""

from __future__ import annotations

import itertools
import json
import string
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import hvac.exceptions
import jsonschema
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vault import ops_kv_generate
from meho_backplane.connectors.vault.ops_kv_generate import (
    GENERATE_OP_ID,
    GENERATE_PARAMETER_SCHEMA,
    GENERATE_RESPONSE_SCHEMA,
    PasswordRuleError,
    PasswordRules,
    generate_with_rules,
    vault_kv_generate,
)
from meho_backplane.connectors.vault.tenant_scope import VaultTenantScopeError
from meho_backplane.connectors.vault.userpass_password import PasswordSecretError
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.settings import get_settings

SENTINEL = "Existing-Value-Do-Not-Echo-1"


class _Kv:
    """``client.secrets.kv.v2`` with versions and ``cas``."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.writes: list[dict[str, Any]] = []
        self.deleted: set[tuple[str, str]] = set()
        self.write_exc: Exception | None = None

    def read_secret_version(
        self, path: str, mount_point: str = "secret", **_kw: Any
    ) -> dict[str, Any]:
        history = self.versions.get((mount_point, path))
        if not history or (mount_point, path) in self.deleted:
            raise hvac.exceptions.InvalidPath()
        return {"data": {"data": dict(history[-1]), "metadata": {"version": len(history)}}}

    def create_or_update_secret(
        self, path: str, secret: dict[str, Any], cas: int | None = None, mount_point: str = "secret"
    ) -> dict[str, Any]:
        if self.write_exc is not None:
            raise self.write_exc
        history = self.versions.setdefault((mount_point, path), [])
        if cas is not None and cas != len(history):
            raise hvac.exceptions.InvalidRequest("check-and-set parameter did not match")
        self.writes.append({"path": path, "cas": cas})
        history.append(dict(secret))
        return {"data": {"version": len(history)}}


class _Client:
    def __init__(self, kv: _Kv) -> None:
        self.secrets = type("S", (), {})()
        self.secrets.kv = type("K", (), {})()  # type: ignore[attr-defined]
        self.secrets.kv.v2 = kv  # type: ignore[attr-defined]


@pytest.fixture
def kv(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Kv]:
    store = _Kv()

    @asynccontextmanager
    async def _client(_operator: Any, _target: Any) -> AsyncIterator[_Client]:
        yield _Client(store)

    monkeypatch.setattr(ops_kv_generate, "vault_client_for_target", _client)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("VAULT_KV_TENANT_SCOPE_PREFIX", raising=False)
    get_settings.cache_clear()
    yield store
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


def _run(params: dict[str, Any]) -> Any:
    return vault_kv_generate(_operator(), None, params)


def _stored(kv: _Kv, path: str = "labs/sso", field: str = "password") -> str:
    return str(kv.versions[("secret", path)][-1][field])


# --- the rules --------------------------------------------------------------


def test_every_value_meets_the_rules_over_many_runs() -> None:
    rules = PasswordRules(
        length=20,
        min_upper=2,
        min_lower=3,
        min_digits=2,
        min_special=1,
        special_chars="!#-_",
        max_repeat=2,
    )
    allowed = string.ascii_letters + string.digits + "!#-_"
    for _ in range(300):
        value = generate_with_rules(rules)
        assert len(value) == 20
        assert sum(c.isupper() for c in value) >= 2
        assert sum(c.islower() for c in value) >= 3
        assert sum(c.isdigit() for c in value) >= 2
        assert sum(c in "!#-_" for c in value) >= 1
        assert set(value) <= set(allowed)
        assert all(not (a == b == c) for a, b, c in zip(value, value[1:], value[2:], strict=False))


def test_max_repeat_one_allows_no_equal_neighbours() -> None:
    rules = PasswordRules(length=40, max_repeat=1)
    for _ in range(100):
        value = generate_with_rules(rules)
        assert all(a != b for a, b in itertools.pairwise(value))


def test_no_special_characters_when_the_set_is_empty() -> None:
    rules = PasswordRules(length=30, special_chars="")
    for _ in range(100):
        assert generate_with_rules(rules).isalnum()


def test_values_differ_between_runs() -> None:
    assert len({generate_with_rules(PasswordRules()) for _ in range(50)}) == 50


def _longest_run(value: str) -> int:
    return max(len(list(group)) for _, group in itertools.groupby(value))


@pytest.mark.parametrize(
    "rules",
    [
        {"length": 128, "min_upper": 0, "min_lower": 0, "min_digits": 128, "max_repeat": 1},
        {"length": 64, "min_upper": 64, "min_lower": 0, "min_digits": 0, "max_repeat": 1},
        {"length": 9, "min_special": 5, "special_chars": "!", "max_repeat": 1},
        {"length": 12, "min_special": 8, "special_chars": "!", "max_repeat": 2},
        {"length": 30, "min_special": 10, "special_chars": "#", "max_repeat": 1},
        {"length": 2, "min_upper": 0, "min_lower": 0, "min_digits": 0, "max_repeat": 1},
    ],
)
def test_strict_rules_that_can_be_met_always_work(rules: dict[str, Any]) -> None:
    parsed = PasswordRules.from_params(rules)
    for _ in range(300):
        value = generate_with_rules(parsed)
        assert len(value) == parsed.length
        assert sum(c.isupper() for c in value) >= parsed.min_upper
        assert sum(c.isdigit() for c in value) >= parsed.min_digits
        assert sum(c in parsed.special_chars for c in value) >= parsed.min_special
        assert _longest_run(value) <= parsed.max_repeat


@pytest.mark.parametrize(
    "rules",
    [
        {"length": 4, "min_upper": 2, "min_lower": 2, "min_digits": 1},
        {"length": 10, "min_special": 1, "special_chars": ""},
        {"length": 10, "special_chars": "a"},
        {"length": 10, "special_chars": "! "},
        {"length": 10, "min_upper": -1},
        {"length": 10, "max_repeat": 0},
        {"length": 9, "min_special": 6, "special_chars": "!", "max_repeat": 1},
        {"length": 12, "min_special": 9, "special_chars": "!", "max_repeat": 2},
    ],
)
def test_rules_that_cannot_be_met_are_refused(rules: dict[str, Any]) -> None:
    with pytest.raises(PasswordRuleError):
        PasswordRules.from_params(rules)


# --- the op -----------------------------------------------------------------


async def test_value_is_in_kv_and_the_result_names_only_the_place(kv: _Kv) -> None:
    result = await _run({"path": "labs/sso", "length": 20, "min_special": 1})
    value = _stored(kv)
    assert len(value) == 20
    assert result == {
        "mount": "secret",
        "path": "labs/sso",
        "field": "password",
        "version": 1,
        "generated": True,
    }
    assert value not in json.dumps(result)
    assert kv.writes[0]["cas"] == 0
    jsonschema.validate(result, GENERATE_RESPONSE_SCHEMA)


async def test_other_fields_at_the_path_stay(kv: _Kv) -> None:
    kv.versions[("secret", "labs/sso")] = [{"username": "svc-user"}]
    result = await _run({"path": "labs/sso"})
    last = kv.versions[("secret", "labs/sso")][-1]
    assert last["username"] == "svc-user"
    assert "password" in last
    assert result["version"] == 2


async def test_second_run_changes_nothing(kv: _Kv) -> None:
    await _run({"path": "labs/sso"})
    before = list(kv.versions[("secret", "labs/sso")])
    result = await _run({"path": "labs/sso"})
    assert kv.versions[("secret", "labs/sso")] == before
    assert result["generated"] is False
    assert result["version"] == 1


async def test_an_existing_value_is_kept_and_not_echoed(kv: _Kv) -> None:
    kv.versions[("secret", "labs/sso")] = [{"password": SENTINEL}]
    result = await _run({"path": "labs/sso"})
    assert _stored(kv) == SENTINEL
    assert result["generated"] is False
    assert SENTINEL not in json.dumps(result)


@pytest.mark.parametrize("current", [12345, 0, False, {"nested": "x"}, ["a"]])
async def test_a_value_that_is_not_text_is_left_alone(kv: _Kv, current: Any) -> None:
    kv.versions[("secret", "labs/sso")] = [{"password": current, "username": "svc-user"}]
    with pytest.raises(PasswordSecretError) as err:
        await _run({"path": "labs/sso"})
    assert kv.writes == []
    assert kv.versions[("secret", "labs/sso")] == [{"password": current, "username": "svc-user"}]
    assert "rotate" in str(err.value)
    assert "12345" not in str(err.value)


async def test_rotate_replaces_a_value_that_is_not_text(kv: _Kv) -> None:
    kv.versions[("secret", "labs/sso")] = [{"password": 12345}]
    result = await _run({"path": "labs/sso", "rotate": True})
    assert result["generated"] is True
    assert isinstance(kv.versions[("secret", "labs/sso")][-1]["password"], str)


async def test_rotate_writes_a_new_version(kv: _Kv) -> None:
    await _run({"path": "labs/sso"})
    first = _stored(kv)
    result = await _run({"path": "labs/sso", "rotate": True})
    assert _stored(kv) != first
    assert result["version"] == 2
    assert result["generated"] is True
    assert kv.writes[-1]["cas"] == 1


async def test_a_custom_field_and_mount(kv: _Kv) -> None:
    result = await _run({"path": "labs/sso", "mount": "kv", "field": "pw"})
    assert ("kv", "labs/sso") in kv.versions
    assert result["field"] == "pw"
    assert result["mount"] == "kv"


async def test_an_empty_value_counts_as_missing(kv: _Kv) -> None:
    kv.versions[("secret", "labs/sso")] = [{"password": ""}]
    result = await _run({"path": "labs/sso"})
    assert result["generated"] is True
    assert _stored(kv) != ""


async def test_a_lost_race_is_an_error_without_a_value(kv: _Kv) -> None:
    kv.write_exc = hvac.exceptions.InvalidRequest("check-and-set parameter did not match")
    with pytest.raises(PasswordSecretError) as err:
        await _run({"path": "labs/sso"})
    assert "labs/sso" in str(err.value)


async def test_a_soft_deleted_secret_is_an_error(kv: _Kv) -> None:
    kv.versions[("secret", "labs/sso")] = [{"password": SENTINEL}]
    kv.deleted.add(("secret", "labs/sso"))
    with pytest.raises(PasswordSecretError) as err:
        await _run({"path": "labs/sso"})
    assert SENTINEL not in str(err.value)


async def test_unmet_rules_write_nothing(kv: _Kv) -> None:
    with pytest.raises(PasswordRuleError):
        await _run({"path": "labs/sso", "length": 2})
    assert kv.writes == []


async def test_another_tenants_path_is_refused(kv: _Kv) -> None:
    other = UUID(int=9)
    with pytest.raises(VaultTenantScopeError):
        await vault_kv_generate(_operator(1), None, {"path": f"tenants/{other}/login"})
    assert kv.writes == []


# --- schema and preview -----------------------------------------------------


def test_schema_accepts_the_rules_and_refuses_unknown_keys() -> None:
    jsonschema.validate(
        {"path": "p", "length": 20, "min_special": 1, "special_chars": "!", "max_repeat": 2},
        GENERATE_PARAMETER_SCHEMA,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"path": "p", "value": "x"}, GENERATE_PARAMETER_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"length": 20}, GENERATE_PARAMETER_SCHEMA)


async def test_preview_shows_path_field_and_rules_never_a_value() -> None:
    builder = _PREVIEW_BUILDERS[GENERATE_OP_ID]
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=None,
        connector_id="vault-1.x",
        params={"path": "labs/sso", "length": 20, "min_special": 1, "rotate": True},
    )
    preview = await builder(ctx)
    assert preview is not None
    assert preview["path"] == "labs/sso"
    assert preview["field"] == "password"
    assert preview["semantics"] == "rotate"
    assert preview["rules"]["length"] == 20
    assert preview["rules"]["min_special"] == 1


async def test_preview_of_unmet_rules_says_so() -> None:
    builder = _PREVIEW_BUILDERS[GENERATE_OP_ID]
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=None,
        connector_id="vault-1.x",
        params={"path": "labs/sso", "length": 2},
    )
    preview = await builder(ctx)
    assert preview is not None
    assert "rules_error" in preview
    assert "rules" not in preview
