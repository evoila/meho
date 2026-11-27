# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every "hard to undo" and "changes rights" mark in code reaches the stored row.

A built-in operation says in code whether a call is hard to undo
(``hard_to_undo``) and whether it changes rights (``changes_rights``). Each
connector passes the two values on in its own ``register_typed_operation``
call, and a start writes them into ``endpoint_descriptor``. A register call
that leaves the two lines out writes no mark, and the approval table
(``tests/snapshots/approval_table.json``) does not hold the two fields. This
test fails then.

The test reads the marks from the definitions in code, not from a copied
list, so a new connector is covered with no change here. It finds them in
the packages of the registered registrars, in three forms:

* an op record: an object with ``op_id``, ``hard_to_undo`` and
  ``changes_rights`` (the dataclasses of most connectors), or a dict with
  ``op_id`` and ``handler`` (the spec dicts of Vault), held in a module
  variable or in a tuple, list or dict of one;
* a call with an ``op_id=`` keyword and its marks written as plain values
  (a register call that names its marks itself, or a call that makes a record);
* a ``for`` loop that unpacks spec tuples of a module variable into names
  such as ``op_id`` and ``hard_to_undo``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.typed_register import (
    _TYPED_OP_REGISTRARS,
    clear_typed_op_snapshot,
)
from meho_backplane.settings import get_settings
from tests.approval_table import register_all_descriptors

#: ``(hard_to_undo, changes_rights)``. No mark is ``(None, False)``.
Mark = tuple[str | None, bool]

_NO_MARK: Mark = (None, False)
_MARK_FIELDS = ("hard_to_undo", "changes_rights")


@pytest.fixture(autouse=True)
def _a_real_start(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every registrar for real, with every operation on, as a start does.

    The suite replays the rows of its first registration pass; that is off
    here, so each start runs the register calls. The policy operations
    register only while ``POLICY_CLASSIFICATION`` is on, so it is on.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.delenv("MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS", raising=False)
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    yield
    clear_typed_op_snapshot()
    get_settings.cache_clear()


@dataclass
class _CodeMarks:
    """The marks in code by operation id, and the ids that code marks in two ways."""

    marks: dict[str, Mark] = field(default_factory=dict)
    conflicts: dict[str, set[Mark]] = field(default_factory=dict)

    def add(self, op_id: str, mark: Mark) -> None:
        known = self.marks.setdefault(op_id, mark)
        if known != mark:
            self.conflicts.setdefault(op_id, {known}).add(mark)


def _registrar_modules() -> list[ModuleType]:
    """Every module in the packages that hold a registered registrar."""
    packages = {registrar.__module__.rpartition(".")[0] for registrar in _TYPED_OP_REGISTRARS}
    modules: dict[str, ModuleType] = {}
    for name in sorted(packages):
        package = importlib.import_module(name)
        modules[name] = package
        for info in pkgutil.walk_packages(package.__path__, f"{name}."):
            modules[info.name] = importlib.import_module(info.name)
    return list(modules.values())


def _record_mark(value: Any) -> tuple[str, Mark] | None:
    """The op id and mark of an op record, or ``None`` when *value* is no op record."""
    if isinstance(value, dict):
        if isinstance(value.get("op_id"), str) and "handler" in value:
            return value["op_id"], (value.get("hard_to_undo"), bool(value.get("changes_rights")))
        return None
    op_id = getattr(value, "op_id", None)
    if isinstance(op_id, str) and all(hasattr(value, name) for name in _MARK_FIELDS):
        return op_id, (value.hard_to_undo, bool(value.changes_rights))
    return None


def _records_in(module: ModuleType) -> Iterator[tuple[str, Mark]]:
    for value in vars(module).values():
        if isinstance(value, (tuple, list, set, frozenset)):
            candidates = [value, *value]
        elif isinstance(value, dict):
            candidates = [value, *value.values()]
        else:
            candidates = [value]
        for candidate in candidates:
            found = _record_mark(candidate)
            if found is not None:
                yield found


def _resolve(node: ast.expr, module: ModuleType) -> Any:
    """The value of a plain value or a module-level name, or ``None``."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return getattr(module, node.id, None)
    return None


def _call_marks(node: ast.Call, module: ModuleType) -> tuple[str, Mark] | None:
    """The mark of a call with ``op_id=`` that writes its marks as plain values.

    A call with ``op_id=`` and no mark (a log line, an error) says nothing about
    the marks, so only a call with at least one mark counts.
    """
    keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
    op_id = _resolve(keywords["op_id"], module) if "op_id" in keywords else None
    marks = [keywords[name] for name in _MARK_FIELDS if name in keywords]
    if not isinstance(op_id, str) or not marks:
        return None
    if not all(isinstance(mark, ast.Constant) for mark in marks):
        return None
    hard_to_undo = (
        _resolve(keywords["hard_to_undo"], module) if "hard_to_undo" in keywords else None
    )
    changes_rights = (
        _resolve(keywords["changes_rights"], module) if "changes_rights" in keywords else False
    )
    return op_id, (hard_to_undo if isinstance(hard_to_undo, str) else None, bool(changes_rights))


def _loop_marks(node: ast.For, module: ModuleType) -> Iterator[tuple[str, Mark]]:
    """The marks of the spec tuples a ``for`` loop unpacks into ``op_id`` and the mark names."""
    if not isinstance(node.target, ast.Tuple) or not isinstance(node.iter, ast.Name):
        return
    names = [item.id if isinstance(item, ast.Name) else None for item in node.target.elts]
    if "op_id" not in names or not any(name in names for name in _MARK_FIELDS):
        return
    specs = getattr(module, node.iter.id, None)
    if not isinstance(specs, (tuple, list)):
        return
    for spec in specs:
        values = dict(zip(names, spec, strict=True))
        yield values["op_id"], (values.get("hard_to_undo"), bool(values.get("changes_rights")))


def _source_marks(module: ModuleType) -> Iterator[tuple[str, Mark]]:
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):
        return
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            found = _call_marks(node, module)
            if found is not None:
                yield found
        elif isinstance(node, ast.For):
            yield from _loop_marks(node, module)


def _code_marks() -> _CodeMarks:
    """The marks in code. An op record wins over a call, as the call may only pass it on."""
    modules = _registrar_modules()
    records = _CodeMarks()
    for module in modules:
        for op_id, mark in _records_in(module):
            records.add(op_id, mark)
    sources = _CodeMarks()
    for module in modules:
        for op_id, mark in _source_marks(module):
            if op_id not in records.marks:
                sources.add(op_id, mark)
    return _CodeMarks(
        marks={**sources.marks, **records.marks},
        conflicts={**sources.conflicts, **records.conflicts},
    )


def _stored_mark(row: EndpointDescriptor) -> tuple[str | None, bool | None]:
    return row.hard_to_undo, row.changes_rights


def _differences(rows: list[EndpointDescriptor], code: _CodeMarks) -> list[str]:
    lines = [
        f"{op_id}: the code gives it different marks: {sorted(marks, key=repr)}"
        for op_id, marks in sorted(code.conflicts.items())
    ]
    for row in sorted(rows, key=lambda item: item.op_id):
        expected = code.marks.get(row.op_id, _NO_MARK)
        stored = _stored_mark(row)
        if stored == expected:
            continue
        if row.op_id not in code.marks:
            lines.append(
                f"{row.op_id}: stored {stored}, but the test finds no definition of it in code. "
                "Give the op a record with op_id, hard_to_undo and changes_rights, "
                "or write the marks as plain values next to op_id="
            )
        else:
            lines.append(f"{row.op_id}: code {expected}, stored {stored}")
    return lines


async def _built_in_rows() -> list[EndpointDescriptor]:
    return [row for row in await register_all_descriptors() if row.tenant_id is None]


def _assert_rows_match_code(rows: list[EndpointDescriptor]) -> None:
    code = _code_marks()
    marked = [row for row in rows if code.marks.get(row.op_id, _NO_MARK) != _NO_MARK]
    # The test reads its marks from code. If that reading broke, few ops would
    # carry one, and a dropped mark would go unseen.
    assert len(marked) > 100, f"only {len(marked)} built-in ops carry a mark in code"
    lines = _differences(rows, code)
    assert not lines, (
        f"{len(lines)} stored operation row(s) differ from the marks in code. "
        "Check that each register call passes hard_to_undo and changes_rights on:\n"
        + "\n".join(lines)
    )


@pytest.mark.asyncio
async def test_each_start_stores_the_marks_of_the_code() -> None:
    # A fresh database.
    _assert_rows_match_code(await _built_in_rows())
    # An upgraded install: its rows are from before the two columns and hold
    # neither value. One start writes the values of the code.
    async with get_sessionmaker()() as session:
        emptied = await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.tenant_id.is_(None))
            .values(hard_to_undo=None, changes_rights=None)
        )
        await session.commit()
    assert emptied.rowcount > 500
    _assert_rows_match_code(await _built_in_rows())
