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
* a connector without its own ``execute`` registers and runs a typed
  operation.

See ``docs/codebase/connector-packages.md``, "The connector kit".
"""

from __future__ import annotations

import importlib
import json
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
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

_UV_LOCK = Path(__file__).resolve().parents[1] / "uv.lock"


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
