# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The connector kit keeps its promise (``meho_connector_kit``).

A connector package imports MEHO only through the kit. These tests check:

* every name in ``backend/connector-kit/kit-names.json`` imports from the kit,
  and the file matches the code. A change fails with a message that names
  the change, the number of the kit version to raise and the command;
* the command ``scripts.write_connector_kit_names`` raises the right number,
  and refuses to remove a name that was not marked as old;
* the kit version is the same in the list file, the kit's ``pyproject.toml``
  and ``uv.lock``;
* both kinds of target MEHO hands a connector have every field of ``Target``;
* a connector written only with the kit's types passes mypy, and reading a
  private target field fails it;
* ``target_cache_key`` keys on the target's tenant, which stays private;
* a connector without its own ``execute`` registers and runs a typed
  operation.

See ``docs/codebase/connector-packages.md``, "The connector kit".
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import meho_connector_kit as kit
import pytest
from scripts.write_connector_kit_names import (
    COMMAND,
    KIT_PYPROJECT,
    Entry,
    build_names,
    check_message,
    describe,
    read_names_file,
    read_pyproject_version,
    write,
)

from meho_backplane.db import models
from meho_backplane.targets import schemas

_BACKEND = Path(__file__).resolve().parents[1]
_UV_LOCK = _BACKEND / "uv.lock"


# ---------------------------------------------------------------------------
# The list file and the code
# ---------------------------------------------------------------------------


def test_every_listed_name_imports_from_the_kit() -> None:
    listed = read_names_file()["names"]
    assert sorted(kit.__all__) == sorted(listed)
    for name in listed:
        module = importlib.import_module("meho_connector_kit")
        assert getattr(module, name) is not None, name


def test_kit_names_file_matches_the_code() -> None:
    message = check_message(read_names_file()["names"], build_names())
    assert message is None, message


def test_kit_version_is_the_same_everywhere() -> None:
    version = read_names_file()["kit_version"]
    assert read_pyproject_version() == version, (
        f"{KIT_PYPROJECT} says {read_pyproject_version()}, kit-names.json says {version}. "
        f"Run `{COMMAND}`."
    )
    lock = tomllib.loads(_UV_LOCK.read_text(encoding="utf-8"))
    locked = [p["version"] for p in lock["package"] if p["name"] == "meho-connector-kit"]
    assert locked == [version], f"uv.lock has kit version {locked}; run `uv lock` in backend/."


# ---------------------------------------------------------------------------
# A change fails the check, and the command raises the right number
# ---------------------------------------------------------------------------


def _list_before(value: str) -> int:
    return len(value)


def _list_after(value: str, limit: int = 10) -> int:
    return min(len(value), limit)


def _names(**functions: Any) -> dict[str, Entry]:
    return {name: describe(function) for name, function in functions.items()}


def test_changed_inputs_fail_with_the_change_the_number_and_the_command() -> None:
    message = check_message(
        _names(list_things=_list_before),
        _names(list_things=_list_after),
    )
    assert message is not None
    assert "changed: list_things" in message
    assert "limit: int = 10" in message
    assert "first number" in message
    assert COMMAND in message


def test_an_added_name_asks_for_the_second_number() -> None:
    message = check_message(_names(a=_list_before), _names(a=_list_before, b=_list_after))
    assert message is not None
    assert "added: b" in message
    assert "second number" in message


@pytest.fixture
def kit_files(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    names_file = tmp_path / "kit-names.json"
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "meho-connector-kit"\nversion = "1.3"\n')
    names_file.write_text(json.dumps({"kit_version": "1.3", "names": _names(a=_list_before)}))
    yield names_file, pyproject


def _written(names_file: Path, pyproject: Path) -> tuple[str, str, dict[str, Entry]]:
    data = read_names_file(names_file)
    return data["kit_version"], read_pyproject_version(pyproject), data["names"]


def test_command_raises_the_second_number_for_an_added_name(
    kit_files: tuple[Path, Path],
) -> None:
    new = _names(a=_list_before, b=_list_after)
    assert write(new, *kit_files) == 0
    assert _written(*kit_files) == ("1.4", "1.4", new)


def test_command_raises_the_first_number_for_a_changed_name(
    kit_files: tuple[Path, Path],
) -> None:
    new = _names(a=_list_after)
    assert write(new, *kit_files) == 0
    assert _written(*kit_files)[:2] == ("2.0", "2.0")


def test_command_raises_the_first_number_for_a_removed_old_name(
    kit_files: tuple[Path, Path],
) -> None:
    names_file, pyproject = kit_files
    old = _names(a=_list_before, b=_list_after)
    old["b"]["old"] = "use a"
    names_file.write_text(json.dumps({"kit_version": "1.3", "names": old}))
    assert write(_names(a=_list_before), names_file, pyproject) == 0
    assert _written(names_file, pyproject)[:2] == ("2.0", "2.0")


def test_command_raises_the_second_number_when_a_name_is_marked_old(
    kit_files: tuple[Path, Path],
) -> None:
    new = _names(a=_list_before)
    new["a"]["old"] = "use b"
    assert write(new, *kit_files) == 0
    assert _written(*kit_files)[:2] == ("1.4", "1.4")


def test_command_keeps_the_version_when_nothing_changed(kit_files: tuple[Path, Path]) -> None:
    assert write(_names(a=_list_before), *kit_files) == 0
    assert _written(*kit_files)[:2] == ("1.3", "1.3")


def test_removing_a_name_that_was_not_old_fails(kit_files: tuple[Path, Path]) -> None:
    names_file, pyproject = kit_files
    before = names_file.read_text()

    message = check_message(_names(a=_list_before), {})
    assert message is not None
    assert "did not mark them as old: a" in message

    assert write({}, names_file, pyproject) == 1
    assert names_file.read_text() == before


# ---------------------------------------------------------------------------
# Both kinds of target have every field of ``Target``
# ---------------------------------------------------------------------------


def _missing_target_fields(kind: type) -> list[str]:
    promised = read_names_file()["names"]["Target"]["parts"]
    model_fields = getattr(kind, "model_fields", {})
    return [name for name in promised if name not in model_fields and not hasattr(kind, name)]


@pytest.mark.parametrize("kind", [models.Target, schemas.Target], ids=["db-row", "read-model"])
def test_both_kinds_of_target_have_every_target_field(kind: type) -> None:
    assert _missing_target_fields(kind) == [], (
        f"{kind.__module__}.{kind.__name__} lost a field that meho_connector_kit.Target "
        "promises to connector packages."
    )


def test_a_target_kind_without_a_field_is_reported() -> None:
    class _NoSecretRef:
        name = "t"
        host = "h"
        extras = None
        auth_model = "per_user"
        verify_tls = True

    assert _missing_target_fields(_NoSecretRef) == ["secret_ref"]


# ---------------------------------------------------------------------------
# A connector typed with the kit passes mypy
# ---------------------------------------------------------------------------


#: A connector that names only kit types. Every kit function that takes a
#: target gets a ``kit.Target``.
_TYPED_CONNECTOR = """\
from typing import Any

import meho_connector_kit as kit


class WidgetConnector(kit.HttpConnector):
    product = "example"
    version = "1.x"
    impl_id = "example-widget"

    async def fingerprint(
        self, target: kit.Target, operator: kit.Operator | None = None
    ) -> kit.FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: kit.Target) -> kit.ProbeResult:
        raise NotImplementedError

    async def auth_headers(self, target: kit.Target, operator: kit.Operator) -> dict[str, str]:
        if not kit.is_acceptable_auth_model(target.auth_model):
            raise kit.ConnectorUpstreamError(
                f"{target.name} must use the shared service account",
                http_status=401,
                upstream_message=None,
            )
        secret = await kit.load_vault_secret_data(target, operator)
        token = kit.strip_credential_value(secret["token"])
        return {"Authorization": f"Bearer {token}"}


async def list_widgets(
    operator: kit.Operator, target: kit.Target, params: dict[str, Any]
) -> dict[str, Any]:
    key = kit.target_cache_key(target)
    extra = await kit.load_param_secret_data(target, operator, param="credential_ref")
    return {
        "key": list(key),
        "fields": sorted(extra),
        "name": target.name,
        "host": target.host,
        "region": target.extras.get("region"),
        "verify_tls": target.verify_tls,
        "secret_ref": target.secret_ref,
        "limit": params.get("limit"),
    }
"""

#: Reads a target field that is not one of the six the kit promises.
_READS_PRIVATE_FIELD = """\
import meho_connector_kit as kit


def tenant_of(target: kit.Target) -> object:
    return target.tenant_id
"""


def _mypy_errors(*files: Path) -> list[str]:
    """Run mypy on *files* with the backend's config and return its error lines.

    ``MYPYPATH`` points at ``src/`` so mypy reads the core modules as
    source. Without it, mypy sees the installed ``meho_backplane`` as a
    package without type information and every kit name becomes ``Any``,
    which would make a check like this pass without checking anything.
    """
    env = {**os.environ, "MYPYPATH": str(_BACKEND / "src")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(_BACKEND / "pyproject.toml"),
            "--no-error-summary",
            "--no-color-output",
            *map(str, files),
        ],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return [line for line in result.stdout.splitlines() if ": error: " in line]


def test_a_connector_typed_with_the_kit_passes_mypy(tmp_path: Path) -> None:
    typed = tmp_path / "typed_connector.py"
    typed.write_text(textwrap.dedent(_TYPED_CONNECTOR), encoding="utf-8")
    private = tmp_path / "reads_private_field.py"
    private.write_text(textwrap.dedent(_READS_PRIVATE_FIELD), encoding="utf-8")

    errors = _mypy_errors(typed, private)

    assert [e for e in errors if typed.name in e] == [], errors
    assert len(errors) == 1, errors
    assert private.name in errors[0] and "tenant_id" in errors[0], errors


# ---------------------------------------------------------------------------
# ``target_cache_key`` keys on the tenant, which stays private
# ---------------------------------------------------------------------------


def test_target_cache_key_keeps_tenants_apart() -> None:
    first, second = _FakeTarget(), _FakeTarget()
    second.tenant_id = uuid.UUID("00000000-0000-0000-0000-00000000beef")
    second.id = first.id

    assert kit.target_cache_key(first) == (str(_TENANT), str(first.id))
    assert kit.target_cache_key(first) != kit.target_cache_key(second)


def test_target_cache_key_refuses_a_target_without_a_tenant() -> None:
    class _SixFields:
        name = "stub"
        host = "stub.example.com"
        extras: ClassVar[dict[str, Any]] = {}
        auth_model = "shared_service_account"
        verify_tls = True
        secret_ref = None

    with pytest.raises(TypeError, match="stub"):
        kit.target_cache_key(_SixFields())


# ---------------------------------------------------------------------------
# A connector without its own ``execute``
# ---------------------------------------------------------------------------


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.connectors.registry import clear_registry
    from meho_backplane.operations import reset_dispatcher_caches
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


class _KitOnlyConnector(kit.Connector):
    """A connector written only against the kit, with no ``execute``."""

    product = "kitdemo"
    version = "1.x"
    impl_id = "kitdemo"

    async def fingerprint(self, target: Any, operator: Any = None) -> kit.FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> kit.ProbeResult:
        raise NotImplementedError


async def _list_widgets(
    operator: kit.Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return {"widgets": [target.name], "limit": params.get("limit")}


class _FakeFingerprint:
    version: str | None = None


class _FakeTarget:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = "kitdemo-target"
        self.host = "kitdemo.example.com"
        self.port = 443
        self.product = "kitdemo"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = None
        self.auth_model = "shared_service_account"


_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0de")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_env")
async def test_connector_without_execute_registers_and_runs_a_typed_operation() -> None:
    from meho_backplane.auth.operator import PrincipalKind, TenantRole
    from meho_backplane.operations import dispatch

    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    kit.register_connector_v2(
        product="kitdemo", version="1.x", impl_id="kitdemo", cls=_KitOnlyConnector
    )
    await kit.register_typed_operation(
        product="kitdemo",
        version="1.x",
        impl_id="kitdemo",
        op_id="kitdemo.widget.list",
        handler=_list_widgets,
        summary="List widgets.",
        description="Lists the widgets of a made-up product.",
        parameter_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        when_to_use=None,
        embedding_service=embedding,
    )
    operator = kit.Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )

    result = await dispatch(
        operator=operator,
        connector_id="kitdemo-1.x",
        op_id="kitdemo.widget.list",
        target=_FakeTarget(),
        params={"limit": 5},
    )

    assert result.status == "ok", result
    assert result.result == {"widgets": ["kitdemo-target"], "limit": 5}


@pytest.mark.asyncio
async def test_default_execute_says_to_use_a_registered_operation() -> None:
    with pytest.raises(NotImplementedError, match="register_typed_operation"):
        await _KitOnlyConnector().execute(_FakeTarget(), "kitdemo.widget.list", {})
