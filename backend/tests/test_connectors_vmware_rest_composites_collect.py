# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vmware-rest composite rows are collected from the package modules.

Each composite module exports ``COMPOSITES``; ``_register.py`` collects them
at start-up. These tests fail loudly when a module on disk is not collected,
when two modules share an op id, and when a row's preview builder or its
"not on vCenter 8.0" reason did not reach the registry.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

from meho_backplane.connectors.vmware_rest import composites
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
    collect_composites,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.operations._preview import _PREVIEW_BUILDERS

_PACKAGE_DIR = Path(composites.__file__).parent


def _modules_on_disk() -> list[ModuleType]:
    names = sorted(
        path.stem
        for path in _PACKAGE_DIR.glob("*.py")
        if path.stem not in {"__init__", "_register"}
    )
    assert names
    return [importlib.import_module(f"{composites.__name__}.{name}") for name in names]


def test_every_module_on_disk_is_collected() -> None:
    """The rows of each module on disk are in the collected tuple, and nothing else is."""
    collected = {spec.op_id: spec for spec in _COMPOSITES}
    from_disk: dict[str, CompositeSpec] = {}
    modules_with_rows = 0
    for module in _modules_on_disk():
        rows = getattr(module, "COMPOSITES", ())
        if rows:
            modules_with_rows += 1
        for row in rows:
            assert isinstance(row, CompositeSpec), module.__name__
            assert collected.get(row.op_id) is row, f"{module.__name__}: {row.op_id} not collected"
            from_disk[row.op_id] = row
    assert modules_with_rows > 1
    assert set(from_disk) == set(collected)


def test_rows_are_sorted_by_op_id_and_unique() -> None:
    op_ids = [spec.op_id for spec in _COMPOSITES]
    assert op_ids == sorted(op_ids)
    assert len(op_ids) == len(set(op_ids))


def test_pkgutil_sees_every_module_file() -> None:
    """The collector walks the package with pkgutil; the walk must match the files on disk."""
    walked = {info.name for info in pkgutil.iter_modules(composites.__path__)}
    on_disk = {path.stem for path in _PACKAGE_DIR.glob("*.py") if path.stem != "__init__"}
    assert walked == on_disk


def test_row_preview_builders_are_registered() -> None:
    """A row's preview builder is the one the approval flow finds for its op id."""
    with_preview = [spec for spec in _COMPOSITES if spec.preview is not None]
    assert with_preview
    for spec in with_preview:
        assert _PREVIEW_BUILDERS.get(spec.op_id) is spec.preview, spec.op_id


def test_not_on_8_0_is_derived_from_the_rows() -> None:
    expected = {spec.op_id: spec.not_on_8_0 for spec in _COMPOSITES if spec.not_on_8_0 is not None}
    assert expected
    assert expected == COMPOSITES_NOT_ON_8_0
    for reason in COMPOSITES_NOT_ON_8_0.values():
        assert reason.strip()


_ROW = textwrap.dedent(
    """
    from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec


    async def handler(*args, **kwargs):
        return {{}}


    COMPOSITES = (
        CompositeSpec(
            op_id="{op_id}",
            handler=handler,
            summary="s",
            description="d",
            parameter_schema={{}},
            response_schema={{}},
            group_key="vm",
            tags=[],
            safety_level="safe",
            requires_approval=False,
        ),
    )
    """
)


def _fake_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]
) -> ModuleType:
    """Build a package under *tmp_path* with the given modules and import it."""
    name = f"fake_composites_{tmp_path.name}".replace("-", "_")
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for module_name, source in files.items():
        (pkg_dir / f"{module_name}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    package = importlib.import_module(name)
    for loaded in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
        monkeypatch.delitem(sys.modules, loaded, raising=False)
    sys.modules[name] = package
    return package


def test_collect_composites_takes_rows_from_every_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _fake_package(
        tmp_path,
        monkeypatch,
        {
            "_zeta": _ROW.format(op_id="x.b"),
            "_alpha": _ROW.format(op_id="x.c"),
            "_helper": "HELPER = 1\n",
            "_beta": _ROW.format(op_id="x.a"),
        },
    )
    rows = collect_composites(package)
    assert [row.op_id for row in rows] == ["x.a", "x.b", "x.c"]


def test_collect_composites_rejects_an_op_id_two_modules_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _fake_package(
        tmp_path,
        monkeypatch,
        {"_one": _ROW.format(op_id="x.dup"), "_two": _ROW.format(op_id="x.dup")},
    )
    with pytest.raises(ValueError, match=r"x\.dup"):
        collect_composites(package)


def test_collect_composites_rejects_a_row_that_is_not_a_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _fake_package(
        tmp_path, monkeypatch, {"_dict_rows": 'COMPOSITES = ({"op_id": "x.a"},)\n'}
    )
    with pytest.raises(TypeError, match="_dict_rows"):
        collect_composites(package)
