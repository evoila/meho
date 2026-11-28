# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A name or a mount that a Vault operation puts into a Vault path stays one plain path value.

* Through the real dispatcher and a stand-in Vault that records every
  request: a bad name or mount is refused as ``invalid_params``, the
  stand-in gets no request, and no approval request is stored.
* A plain name and a nested mount still reach the expected Vault path.
* The guard: every registered Vault operation's name-like parameter is in
  ``VAULT_PATH_PARAMS`` (and then has the check and the schema pattern) or
  in ``VAULT_NAME_PARAMS_NOT_IN_PATH``.
* ``vault.auth.userpass.update_password`` is ``dangerous``.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import hvac
import pytest
import requests
from jsonschema import Draft202012Validator
from sqlalchemy import select

import meho_backplane.connectors.vault as vault_pkg
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.vault.path_params import (
    MOUNT,
    NAME,
    VAULT_NAME_PARAMS_NOT_IN_PATH,
    VAULT_PATH_PARAMS,
    path_param,
    path_part_problem,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import dispatch
from meho_backplane.operations._validate import _PARAMS_CHECKS, params_check_errors
from meho_backplane.operations.approval_queue import (
    ApprovalGroupForbiddenError,
    RightsChangeApprovalError,
    approval_risk_level,
    approve_request,
)
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

_TENANT = UUID(int=7)
_VAULT = "http://vault.test.invalid"

#: Name values the check refuses.
BAD_NAMES = [
    "../x",
    "..",
    ".",
    "a/b",
    "a?x=1",
    "a#b",
    "%2e%2e",
    "a\\b",
    "a\nb",
]
#: Mount values the check refuses.
BAD_MOUNTS = ["a/../b", "..", "a//b", "a?x=1", "%2e%2e/x", "a#b", "a\\b", "a\x00b"]

#: The other params each operation needs, so only the path value is wrong.
_OTHER_PARAMS: dict[str, dict[str, Any]] = {
    "vault.auth.userpass.write": {"password": "s3cret-value"},
    "vault.auth.userpass.update_password": {"password": "s3cret-value"},
    "vault.sys.policy.write": {"policy": 'path "secret/x" { capabilities = ["read"] }'},
    "vault.sys.auth.enable": {"method_type": "userpass"},
    "vault.sys.mounts.enable": {"backend_type": "kv"},
    "vault.token.create_and_store": {"store": {"vault_kv": {"path": "x"}}},
}

_NAME_LIKE = re.compile(r"^(mount|path|name|username|accessor)$|_(name|id|mount|ref|accessor)$")


# ---------------------------------------------------------------------------
# The stand-in Vault
# ---------------------------------------------------------------------------


class _RecordingVault(requests.adapters.BaseAdapter):
    """Answers every request with 200 and records its method and URL."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, str]] = []

    def send(self, request: requests.PreparedRequest, **_kwargs: Any) -> requests.Response:
        self.requests.append((str(request.method), str(request.url)))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        body = {"data": {"keys": ["k"], "id": "entity-1", "rules": "", "name": "n"}}
        response._content = json.dumps(body).encode()
        response.url = str(request.url)
        response.request = request
        return response

    def close(self) -> None:
        return None

    def paths(self) -> list[str]:
        return [url.removeprefix(_VAULT) for _, url in self.requests]


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingVault]:
    recorder = _RecordingVault()

    @asynccontextmanager
    async def _client(_operator: Any, _target: Any) -> AsyncIterator[hvac.Client]:
        client = hvac.Client(url=_VAULT, token="stand-in-token")
        client.adapter.session.mount("http://", recorder)
        yield client

    for info in pkgutil.iter_modules(vault_pkg.__path__):
        module = importlib.import_module(f"{vault_pkg.__name__}.{info.name}")
        if hasattr(module, "vault_client_for_target"):
            monkeypatch.setattr(module, "vault_client_for_target", _client)
    yield recorder


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
    await run_typed_op_registrars(embedding_service=service)
    yield
    get_settings.cache_clear()


def _operator(role: TenantRole = TenantRole.OPERATOR, sub: str = "op-caller") -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
    )


async def _dispatch(op_id: str, params: dict[str, Any], *, approved: bool = False) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id="vault-1.x",
        op_id=op_id,
        target=None,
        params=params,
        _approved=approved,
    )


def _refused(result: Any) -> bool:
    return result.status == "error" and (result.extras or {}).get("error_code") == "invalid_params"


def _refused_for(result: Any, field: str) -> bool:
    """The call is refused, and a validation error names ``field``."""
    errors = (result.extras or {}).get("validation_errors") or []
    return _refused(result) and any(e.get("path") in (field, f"$.{field}") for e in errors)


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


def _base_params(op_id: str) -> dict[str, Any]:
    params = {name: "plain-name" for name, kind in VAULT_PATH_PARAMS[op_id].items() if kind == NAME}
    return params | _OTHER_PARAMS.get(op_id, {})


# ---------------------------------------------------------------------------
# The shared check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", BAD_NAMES)
def test_a_bad_name_is_refused(value: str) -> None:
    assert path_part_problem(value, NAME) is not None
    with pytest.raises(ValueError, match="username must be"):
        path_param({"username": value}, "username", NAME)


@pytest.mark.parametrize("value", BAD_MOUNTS)
def test_a_bad_mount_is_refused(value: str) -> None:
    assert path_part_problem(value, MOUNT) is not None


@pytest.mark.parametrize("value", ["alice", "svc.account", "a..b", "Team A", " alice "])
def test_a_plain_name_passes(value: str) -> None:
    assert path_param({"username": value}, "username", NAME) == value.strip()


@pytest.mark.parametrize("value", ["userpass", "team-a/userpass", "/approle/", "auth.v2"])
def test_a_plain_mount_passes(value: str) -> None:
    assert path_param({"mount": value}, "mount", MOUNT) == value.strip().strip("/")


def test_a_missing_mount_takes_the_default() -> None:
    assert path_param({}, "mount", MOUNT, default="userpass") == "userpass"
    assert path_param({"mount": None}, "mount", MOUNT, default="jwt") == "jwt"


def test_the_message_never_holds_the_value() -> None:
    with pytest.raises(ValueError) as caught:
        path_param({"username": "../secret-marker"}, "username", NAME)
    assert "secret-marker" not in str(caught.value)


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    ("op_id", "field"),
    [("vault.auth.userpass.read", "username"), ("vault.identity.entity.read", "entity_id")],
)
async def test_a_read_with_dot_dot_sends_nothing(
    vault: _RecordingVault, op_id: str, field: str
) -> None:
    result = await _dispatch(op_id, {field: "../x"})
    assert _refused(result), result
    assert result.extras["validation_errors"][0]["path"] in (field, f"$.{field}")
    assert vault.requests == []


@pytest.mark.usefixtures("registered")
async def test_a_delete_with_dot_dot_stores_no_approval_request(vault: _RecordingVault) -> None:
    result = await _dispatch("vault.auth.userpass.delete", {"username": "a/../b"})
    assert _refused(result), result
    assert await _approval_rows() == []
    assert vault.requests == []


_PATH_CASES = [
    (op_id, field, kind)
    for op_id, fields in VAULT_PATH_PARAMS.items()
    for field, kind in fields.items()
]


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(("op_id", "field", "kind"), _PATH_CASES)
async def test_every_path_value_is_refused_before_anything_is_stored_or_sent(
    vault: _RecordingVault, op_id: str, field: str, kind: str
) -> None:
    for value in BAD_NAMES if kind == NAME else BAD_MOUNTS:
        result = await _dispatch(op_id, _base_params(op_id) | {field: value})
        assert _refused_for(result, field), (value, result)
    assert vault.requests == []
    assert await _approval_rows() == []


_WORKS = [
    ("vault.auth.userpass.read", {"username": "alice", "mount": "team-a/userpass"},
     "/v1/auth/team-a/userpass/users/alice"),
    ("vault.auth.approle.read", {"role_name": "ci", "mount": "team-a/approle"},
     "/v1/auth/team-a/approle/role/ci"),
    ("vault.auth.jwt.role.read", {"role_name": "ci", "mount": "team-a/jwt"},
     "/v1/auth/team-a/jwt/role/ci"),
    ("vault.identity.entity.read", {"entity_id": "entity-1"}, "/v1/identity/entity/id/entity-1"),
    ("vault.identity.group.read", {"name": "ops"}, "/v1/identity/group/name/ops"),
    ("vault.identity.oidc.key.read", {"name": "meho"}, "/v1/identity/oidc/key/meho"),
    ("vault.token.role.read", {"role_name": "ci"}, "/v1/auth/token/roles/ci"),
    ("vault.sys.policy.read", {"name": "ops"}, "/v1/sys/policy/ops"),
]  # fmt: skip


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(("op_id", "params", "path"), _WORKS)
async def test_a_plain_name_and_a_nested_mount_still_work(
    vault: _RecordingVault, op_id: str, params: dict[str, Any], path: str
) -> None:
    result = await _dispatch(op_id, params)
    assert result.status == "ok", result
    assert path in vault.paths()


# ---------------------------------------------------------------------------
# The guard: every Vault operation
# ---------------------------------------------------------------------------


async def _vault_schemas() -> dict[str, dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id.like("vault.%"))
            )
        ).scalars()
        return {row.op_id: row.parameter_schema or {} for row in rows}


def _guard_problems(schemas: dict[str, dict[str, Any]]) -> list[str]:
    """Every way a Vault operation can put a name-like value into a path unchecked."""
    problems: list[str] = []
    for op_id, schema in sorted(schemas.items()):
        listed = VAULT_PATH_PARAMS.get(op_id, {})
        other = VAULT_NAME_PARAMS_NOT_IN_PATH.get(op_id, frozenset())
        properties = schema.get("properties", {})
        for field, prop in properties.items():
            if prop.get("type") != "string" or not _NAME_LIKE.search(field):
                continue
            if (field in listed) == (field in other):
                problems.append(f"{op_id}.{field}: in neither list, or in both")
        for field, kind in listed.items():
            if field not in properties:
                problems.append(f"{op_id}.{field}: listed but not a parameter")
                continue
            checker = Draft202012Validator({"pattern": properties[field].get("pattern", "")})
            for value in BAD_NAMES[:8] if kind == NAME else BAD_MOUNTS[:7]:
                if checker.is_valid(value):
                    problems.append(f"{op_id}.{field}: the schema pattern lets {value!r} pass")
                if not params_check_errors(op_id, _base_params(op_id) | {field: value}):
                    problems.append(f"{op_id}.{field}: the params check lets {value!r} pass")
    for op_id in VAULT_PATH_PARAMS:
        if op_id not in schemas:
            problems.append(f"{op_id}: listed but not registered")
    return problems


@pytest.mark.usefixtures("registered")
async def test_every_vault_operation_checks_its_path_values() -> None:
    schemas = await _vault_schemas()
    assert len(schemas) > 50
    assert _guard_problems(schemas) == []


@pytest.mark.usefixtures("registered")
async def test_the_guard_fails_when_the_check_is_taken_off_one_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = await _vault_schemas()
    monkeypatch.delitem(_PARAMS_CHECKS, "vault.auth.userpass.read")
    problems = _guard_problems(schemas)
    assert problems
    assert all(p.startswith("vault.auth.userpass.read.") for p in problems)


@pytest.mark.usefixtures("registered")
async def test_the_guard_fails_for_a_name_that_is_in_no_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = await _vault_schemas()
    monkeypatch.delitem(VAULT_PATH_PARAMS, "vault.identity.group.delete")
    assert _guard_problems(schemas) == [
        "vault.identity.group.delete.name: in neither list, or in both"
    ]


# ---------------------------------------------------------------------------
# The risk level of update_password
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("registered")
async def test_update_password_is_dangerous_and_a_non_admin_may_not_approve_it(
    vault: _RecordingVault,
) -> None:
    async with get_sessionmaker()() as session:
        levels = (
            await session.execute(
                select(EndpointDescriptor.safety_level).where(
                    EndpointDescriptor.op_id == "vault.auth.userpass.update_password"
                )
            )
        ).scalars()
        assert set(levels) == {"dangerous"}

    result = await _dispatch(
        "vault.auth.userpass.update_password", {"username": "alice", "password": "s3cret-value"}
    )
    assert result.status == "awaiting_approval", result
    (row,) = await _approval_rows()
    assert approval_risk_level(row) == "dangerous"

    reviewer = _operator(TenantRole.OPERATOR, sub="op-reviewer")
    async with get_sessionmaker()() as session:
        with pytest.raises((ApprovalGroupForbiddenError, RightsChangeApprovalError)):
            await approve_request(session, row.id, operator=reviewer)
    assert vault.requests == []
