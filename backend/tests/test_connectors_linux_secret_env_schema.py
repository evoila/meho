# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.script.run``: the ``secret_env`` schema is checked before approval (#3554).

The ``secret_env`` schema carries ``propertyNames`` (the POSIX name pattern) and
``maxProperties`` (the entry cap). The dispatcher checks the schema before the
policy gate. So a bad name or too many entries fail at once with
``invalid_params``, and no approval request is made. The handler check
(``_validate_secret_env``) stays as the second line and keeps the rules a
schema cannot say (reserved names, the clash with ``env``, the reference shape).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

import meho_backplane.connectors.linux  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops_write import (
    _MAX_SECRET_ENV,
    _RESERVED_ENV_NAMES,
    WRITE_OPS,
    LinuxWriteSafetyError,
    _validate_secret_env,
)
from meho_backplane.operations._validate import validate_params
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "linux-ssh-1.x"
_TARGET_NAME = "linux-secret-env-schema"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003554")
_OPERATOR = Operator(
    sub="linux-secret-env-schema-test",
    name="Linux secret_env Schema Operator",
    email=None,
    raw_jwt="<linux-secret-env-schema-raw-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)
_REF = f"tenants/{_TENANT_ID}/app/db#cred"

_BAD_NAMES = (
    "1LEADING_DIGIT",
    "has-dash",
    "has space",
    "",
    "PA;TH",
    "ÄNAME",
    "APP_DB_CRED\n",
    "\nAPP_DB_CRED",
)
_GOOD_NAMES = ("APP_DB_CRED", "_x", "a1", "Z")


def _schema() -> dict[str, Any]:
    op = next(op for op in WRITE_OPS if op.op_id == "linux.script.run")
    return op.parameter_schema


def _errors(secret_env: dict[str, str]) -> list[dict[str, Any]]:
    return validate_params(_schema(), {"script": "echo hi", "secret_env": secret_env})


# -- schema level -----------------------------------------------------------


@pytest.mark.parametrize("name", _BAD_NAMES)
def test_schema_rejects_a_bad_secret_env_name(name: str) -> None:
    errors = _errors({name: _REF})
    assert errors, name
    assert {e["validator"] for e in errors} == {"pattern"}
    assert {e["path"] for e in errors} == {"$.secret_env"}
    # The answer names the rule, never the rejected name.
    if name:
        assert name not in str(errors)
    # The handler check refuses the same name.
    with pytest.raises(LinuxWriteSafetyError):
        _validate_secret_env({name: _REF}, env_names=set())


@pytest.mark.parametrize("name", _GOOD_NAMES)
def test_schema_accepts_a_good_secret_env_name(name: str) -> None:
    assert _errors({name: _REF}) == []
    assert _validate_secret_env({name: _REF}, env_names=set()) == {
        name: (f"tenants/{_TENANT_ID}/app/db", "cred")
    }


def test_schema_enforces_the_entry_cap() -> None:
    at_cap = {f"V{i}": _REF for i in range(_MAX_SECRET_ENV)}
    assert _errors(at_cap) == []
    over_cap = {f"V{i}": _REF for i in range(_MAX_SECRET_ENV + 1)}
    errors = _errors(over_cap)
    assert [e["validator"] for e in errors] == ["maxProperties"]


def test_reserved_names_stay_a_handler_check() -> None:
    """A reserved name passes the schema; the handler still refuses it."""
    for reserved in _RESERVED_ENV_NAMES:
        assert _errors({reserved: _REF}) == []
        with pytest.raises(LinuxWriteSafetyError, match="reserved"):
            _validate_secret_env({reserved: _REF}, env_names=set())


# -- dispatcher: refused before the call waits for approval -----------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def linux_registered() -> AsyncIterator[None]:
    """Register the linux typed ops and seed one linux target in the test DB."""
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import Target as TargetORM
    from meho_backplane.operations import reset_dispatcher_caches
    from meho_backplane.operations.dispatcher import set_default_reducer
    from meho_backplane.operations.reducer import PassThroughReducer

    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    await LinuxSshConnector.register_operations()

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="linux",
                host="linux-host.test.invalid",
                port=22,
                fqdn=None,
                secret_ref="kv/dev/linux/secret-env-schema",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "1.x"},
                notes="seeded by test_connectors_linux_secret_env_schema",
            )
        )
        await session.commit()
    yield
    reset_dispatcher_caches()


async def _dispatch(secret_env: dict[str, str]) -> dict[str, Any]:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id="linux.script.run",
        target=target,
        params={"script": "echo hi", "secret_env": secret_env},
        _approved=False,
    )
    return result.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_env",
    [
        {"has-dash": _REF},
        {"APP_DB_CRED\n": _REF},
        {f"V{i}": _REF for i in range(_MAX_SECRET_ENV + 1)},
    ],
    ids=["bad-name", "trailing-newline", "over-cap"],
)
async def test_bad_secret_env_fails_at_dispatch_not_after_approval(
    linux_registered: None, secret_env: dict[str, str]
) -> None:
    dumped = await _dispatch(secret_env)
    assert dumped["status"] == "error", dumped
    assert dumped["extras"]["error_code"] == "invalid_params"
    assert "approval_request_id" not in dumped["extras"]


@pytest.mark.asyncio
async def test_good_secret_env_still_waits_for_approval(linux_registered: None) -> None:
    dumped = await _dispatch({"APP_DB_CRED": _REF})
    assert dumped["status"] == "awaiting_approval", dumped
    assert dumped["extras"]["approval_request_id"]
