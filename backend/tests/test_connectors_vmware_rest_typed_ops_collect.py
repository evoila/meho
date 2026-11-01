# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard: every row of every ``typed_ops_*`` module reaches ``VMWARE_TYPED_OPS``.

``typed_ops.py`` keeps no list of operations. It finds the ``typed_ops_*``
modules of the package at import and joins their ``TYPED_OPS`` rows. These
tests fail loudly when a module is not found, when a module defines a row
it does not export, and when two modules clash on an op id or a group text.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from meho_backplane.connectors.vmware_rest import typed_ops
from meho_backplane.connectors.vmware_rest.typed_ops import (
    TYPED_OPS_MODULE_PREFIX,
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
    VmwareTypedOp,
    _collect_typed_ops,
    typed_op_module_names,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vm_info import VMWARE_VM_INFO_OP

_PACKAGE = typed_ops.__name__.rpartition(".")[0]


def _module(name: str) -> ModuleType:
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _rows_defined_in(module: ModuleType) -> dict[str, VmwareTypedOp]:
    """Every ``VmwareTypedOp`` at module level, on its own or inside a tuple."""
    found: dict[str, VmwareTypedOp] = {}
    for value in vars(module).values():
        if isinstance(value, VmwareTypedOp):
            found[value.op_id] = value
        elif isinstance(value, tuple | list):
            found.update((op.op_id, op) for op in value if isinstance(op, VmwareTypedOp))
    return found


def _stranded_rows(module_names: tuple[str, ...], collected: tuple[VmwareTypedOp, ...]) -> set[str]:
    """Op ids a module defines that the collected tuple does not hold (by identity)."""
    collected_ids = {id(op) for op in collected}
    return {
        op_id
        for name in module_names
        for op_id, op in _rows_defined_in(_module(name)).items()
        if id(op) not in collected_ids
    }


def _fake_module(name: str, **attrs: object) -> ModuleType:
    module = ModuleType(f"{_PACKAGE}.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def test_the_collector_finds_every_typed_ops_module_on_disk() -> None:
    package_dir = Path(typed_ops.__file__).parent
    on_disk = sorted(
        path.stem
        for path in package_dir.glob(f"{TYPED_OPS_MODULE_PREFIX}*.py")
        if path.stem != "typed_ops"
    )
    assert on_disk
    assert list(typed_op_module_names()) == on_disk


def test_every_row_a_module_defines_is_collected() -> None:
    assert _stranded_rows(typed_op_module_names(), VMWARE_TYPED_OPS) == set()


def test_a_module_that_is_left_out_fails_the_guard() -> None:
    names = typed_op_module_names()
    without_vm_info = tuple(name for name in names if name != "typed_ops_vm_info")
    assert len(without_vm_info) == len(names) - 1
    collected, _ = _collect_typed_ops(_module(name) for name in without_vm_info)
    assert "vmware.vm.info" not in {op.op_id for op in collected}
    assert _stranded_rows(names, collected) == {"vmware.vm.info"}


def test_rows_are_sorted_by_op_id_and_unique() -> None:
    op_ids = [op.op_id for op in VMWARE_TYPED_OPS]
    assert op_ids == sorted(op_ids)
    assert len(op_ids) == len(set(op_ids))


def test_every_group_key_has_a_when_to_use_text() -> None:
    for op in VMWARE_TYPED_OPS:
        if op.group_key is None:
            continue
        text = VMWARE_TYPED_WHEN_TO_USE_BY_GROUP.get(op.group_key)
        assert isinstance(text, str) and text.strip(), op.op_id


def test_helper_modules_without_rows_are_skipped() -> None:
    helper = _fake_module("typed_ops_helper", SOME_CONSTANT="x")
    collected, texts = _collect_typed_ops([helper])
    assert collected == ()
    assert texts == {}


def test_the_same_op_id_in_two_modules_fails_at_import() -> None:
    first = _fake_module("typed_ops_first", TYPED_OPS=(VMWARE_VM_INFO_OP,))
    second = _fake_module("typed_ops_second", TYPED_OPS=(VMWARE_VM_INFO_OP,))
    with pytest.raises(ValueError, match="defined twice"):
        _collect_typed_ops([first, second])


def test_one_group_key_with_two_texts_fails_at_import() -> None:
    first = _fake_module("typed_ops_first", WHEN_TO_USE_BY_GROUP={"g": "one text"})
    second = _fake_module("typed_ops_second", WHEN_TO_USE_BY_GROUP={"g": "another text"})
    with pytest.raises(ValueError, match="two when_to_use texts"):
        _collect_typed_ops([first, second])


def test_one_group_key_with_the_same_text_in_two_modules_is_fine() -> None:
    first = _fake_module("typed_ops_first", WHEN_TO_USE_BY_GROUP={"g": "same text"})
    second = _fake_module("typed_ops_second", WHEN_TO_USE_BY_GROUP={"g": "same text"})
    _, texts = _collect_typed_ops([first, second])
    assert texts == {"g": "same text"}
