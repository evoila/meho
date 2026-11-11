# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``linux.script.run``: the ``env`` and ``secret_env`` schemas are checked before approval.

The ``secret_env`` schema carries ``propertyNames`` (the POSIX name pattern) and
``maxProperties`` (the entry cap). Both ``env`` and ``secret_env`` refuse the
reserved names: the wrapper's own variables and the names that control how the
interpreter starts. The dispatcher checks the schema before the policy gate.
So a bad or reserved name or too many entries fail at once with
``invalid_params``, and no approval request is made. The handler checks
(``_validate_env``, ``_validate_secret_env``) stay as the second line and keep
the rules a schema cannot say (the clash with ``env``, the reference shape).
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops_write import (
    _MAX_SECRET_ENV,
    _RESERVED_ENV_NAMES,
    _RESERVED_ENV_PREFIXES,
    WRITE_OPS,
    LinuxWriteSafetyError,
    _is_reserved_env_name,
    _validate_env,
    _validate_secret_env,
    build_script_run_wrapper,
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


#: Names that change how the script starts or which file runs. Both ``env``
#: and ``secret_env`` refuse them, in the schema and in the handler.
_RESERVED_SAMPLES = (
    "tmp",
    "args",
    "f",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "PATH",
    "IFS",
    "RANDOM",
    "SRANDOM",
    "OPTIND",
    "HISTCMD",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PERL5OPT",
    "RUBYOPT",
    "NODE_OPTIONS",
)

#: Names close to a reserved one that stay allowed (env names are case-sensitive).
_NEAR_RESERVED_ALLOWED = ("TMP", "APP_PATH", "bash_env", "MY_LD_PRELOAD", "ENVIRONMENT")


def _env_errors(env: dict[str, str]) -> list[dict[str, Any]]:
    return validate_params(_schema(), {"script": "echo hi", "env": env})


def test_every_reserved_name_is_refused_by_both_checks() -> None:
    names = sorted(_RESERVED_ENV_NAMES) + [p + "X" for p in _RESERVED_ENV_PREFIXES]
    for name in names:
        assert [e["validator"] for e in _errors({name: _REF})] == ["not"], name
        assert [e["validator"] for e in _env_errors({name: "x"})] == ["not"], name
        with pytest.raises(LinuxWriteSafetyError, match="reserved"):
            _validate_secret_env({name: _REF}, env_names=set())
        with pytest.raises(LinuxWriteSafetyError, match="reserved"):
            _validate_env({name: "x"})


@pytest.mark.parametrize("name", _RESERVED_SAMPLES)
def test_schema_refuses_a_reserved_env_name(name: str) -> None:
    errors = _env_errors({name: "$(id)"})
    assert errors, name
    assert {e["validator"] for e in errors} == {"not"}
    assert {e["path"] for e in errors} == {"$.env"}
    # The answer names neither the rejected name nor its value. (Very short
    # names such as "f" appear in any English text, so they are skipped.)
    if len(name) > 3:
        assert name not in str(errors)
    assert "$(id)" not in str(errors)
    with pytest.raises(LinuxWriteSafetyError, match="reserved"):
        _validate_env({name: "$(id)"})


@pytest.mark.parametrize("name", _RESERVED_SAMPLES)
def test_schema_refuses_a_reserved_secret_env_name(name: str) -> None:
    errors = _errors({name: _REF})
    assert {e["validator"] for e in errors} == {"not"}
    assert {e["path"] for e in errors} == {"$.secret_env"}
    with pytest.raises(LinuxWriteSafetyError, match="reserved"):
        _validate_secret_env({name: _REF}, env_names=set())


@pytest.mark.parametrize("name", _NEAR_RESERVED_ALLOWED + _GOOD_NAMES)
def test_names_close_to_a_reserved_one_stay_allowed(name: str) -> None:
    assert _env_errors({name: "v"}) == []
    assert _validate_env({name: "v"}) == {name: "v"}
    assert _errors({name: _REF}) == []


@pytest.mark.parametrize("name", _BAD_NAMES)
def test_schema_rejects_a_bad_env_name(name: str) -> None:
    errors = _env_errors({name: "v"})
    assert errors, name
    assert {e["path"] for e in errors} == {"$.env"}
    with pytest.raises(LinuxWriteSafetyError):
        _validate_env({name: "v"})


#: An env value that runs a command when bash evaluates it as arithmetic.
#: ``tmp`` is set by the wrapper before the export lines, so ``set -u`` does
#: not stop the evaluation first. Only the command prints the marker: bash may
#: repeat the value text in a warning (for example a bad ``LC_*`` locale), and
#: the value text does not contain the marker.
_ARITHMETIC_PROBE = "tmp[$(echo MEHO_PROBE_$((6 * 7)) >&2)]"
_PROBE_MARKER = "MEHO_PROBE_42"


def _names_that_run_the_probe(names: list[str]) -> list[str]:
    """Return the names whose export line in the real wrapper runs the probe."""
    ran = []
    for name in names:
        wrapper = build_script_run_wrapper(
            "/bin/true",
            "true",
            arguments=None,
            working_directory=None,
            env={name: _ARITHMETIC_PROBE},
        )
        proc = subprocess.run(
            ["bash", "-c", wrapper], capture_output=True, text=True, timeout=10, check=False
        )
        if _PROBE_MARKER in proc.stderr:
            ran.append(name)
    return ran


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_no_allowed_env_name_runs_its_value_in_the_wrapper() -> None:
    """Every bash variable that runs its value at ``export`` is reserved.

    Each name bash knows is exported once with a value that runs a command
    when bash evaluates it as arithmetic. The names that run it must all be
    reserved, so the value never runs before the reviewed script.
    """
    known = subprocess.run(
        ["bash", "-c", "compgen -v"], capture_output=True, text=True, check=True
    ).stdout.split()
    candidates = sorted(set(known) | {"HISTCMD", "OPTIND", "RANDOM", "SRANDOM"})
    ran = _names_that_run_the_probe(candidates)
    # The probe works: bash runs it for RANDOM on every supported version.
    assert "RANDOM" in ran
    assert [name for name in ran if not _is_reserved_env_name(name)] == []
    # A plain name keeps the value as text.
    assert _names_that_run_the_probe(["APP_MODE"]) == []


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


async def _dispatch(
    secret_env: dict[str, str], env: dict[str, str] | None = None
) -> dict[str, Any]:
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
        params={"script": "echo hi", "secret_env": secret_env, "env": env or {}},
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
        {"BASH_ENV": _REF},
        {"tmp": _REF},
    ],
    ids=["bad-name", "trailing-newline", "over-cap", "reserved-bash-env", "reserved-tmp"],
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [{"BASH_ENV": "$(id)"}, {"tmp": "/x"}, {"LD_PRELOAD": "/x.so"}, {"PATH": "/x"}],
    ids=["bash-env", "tmp", "ld-preload", "path"],
)
async def test_reserved_env_name_fails_at_dispatch_not_after_approval(
    linux_registered: None, env: dict[str, str]
) -> None:
    dumped = await _dispatch({}, env)
    assert dumped["status"] == "error", dumped
    assert dumped["extras"]["error_code"] == "invalid_params"
    assert "approval_request_id" not in dumped["extras"]


@pytest.mark.asyncio
async def test_good_env_still_waits_for_approval(linux_registered: None) -> None:
    dumped = await _dispatch({}, {"APP_MODE": "blue"})
    assert dumped["status"] == "awaiting_approval", dumped
    assert dumped["extras"]["approval_request_id"]
