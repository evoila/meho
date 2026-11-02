# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Kubernetes op rows are collected from the package modules.

Each ``ops_<feature>.py`` module exports ``OPS``; ``ops.py`` collects them
at start-up. These tests fail loudly when a module on disk holds rows that
are not collected, when two modules share an op id, and when a row is not
a :class:`KubernetesOp`.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import textwrap
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import pytest

from meho_backplane.connectors import kubernetes
from meho_backplane.connectors.kubernetes.ops import (
    KUBERNETES_OPS,
    OPS_MODULE_PREFIX,
    KubernetesOp,
    collect_ops,
)

_PACKAGE_DIR = Path(kubernetes.__file__).parent


def _modules_on_disk() -> list[ModuleType]:
    names = sorted(path.stem for path in _PACKAGE_DIR.glob("*.py") if path.stem != "__init__")
    assert names
    return [importlib.import_module(f"{kubernetes.__name__}.{name}") for name in names]


def _rows_in(module: ModuleType) -> list[KubernetesOp]:
    """Every row a module holds at top level, alone or in a tuple."""
    rows: list[KubernetesOp] = []
    for value in vars(module).values():
        if isinstance(value, KubernetesOp):
            rows.append(value)
        elif isinstance(value, tuple):
            rows.extend(item for item in value if isinstance(item, KubernetesOp))
    return rows


def _uncollected_rows(
    modules: Iterable[ModuleType], collected: Iterable[KubernetesOp]
) -> list[str]:
    """``module: op_id`` for each row a module holds that is not in *collected*."""
    by_id = {op.op_id: op for op in collected}
    return sorted(
        {
            f"{module.__name__}: {row.op_id}"
            for module in modules
            for row in _rows_in(module)
            if by_id.get(row.op_id) is not row
        }
    )


def test_every_row_on_disk_is_collected() -> None:
    """A row in any module of the package is in KUBERNETES_OPS (export it as ``OPS``)."""
    assert _uncollected_rows(_modules_on_disk(), KUBERNETES_OPS) == []


def test_every_collected_row_comes_from_an_ops_module() -> None:
    from_disk: dict[str, KubernetesOp] = {}
    modules_with_rows = 0
    for module in _modules_on_disk():
        rows = getattr(module, "OPS", ())
        if not rows:
            continue
        assert module.__name__.rpartition(".")[2].startswith(OPS_MODULE_PREFIX)
        modules_with_rows += 1
        for row in rows:
            from_disk[row.op_id] = row
    assert modules_with_rows > 1
    assert set(from_disk) == {op.op_id for op in KUBERNETES_OPS}


def test_rows_are_sorted_by_op_id_and_unique() -> None:
    op_ids = [op.op_id for op in KUBERNETES_OPS]
    assert op_ids == sorted(op_ids)
    assert len(op_ids) == len(set(op_ids))


def test_pkgutil_sees_every_module_file() -> None:
    """The collector walks the package with pkgutil; the walk must match the files on disk."""
    walked = {info.name for info in pkgutil.iter_modules(kubernetes.__path__) if not info.ispkg}
    on_disk = {path.stem for path in _PACKAGE_DIR.glob("*.py") if path.stem != "__init__"}
    assert walked == on_disk


_ROW = textwrap.dedent(
    """
    from meho_backplane.connectors.kubernetes.ops import KubernetesOp

    ROW = KubernetesOp(
        op_id="{op_id}",
        handler_attr="about",
        summary="s",
        description="d",
        parameter_schema={{}},
        response_schema=None,
        group_key=None,
        tags=(),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=None,
    )
    """
)


def _fake_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]
) -> ModuleType:
    """Build a package under *tmp_path* with the given modules and import it."""
    name = f"fake_k8s_ops_{tmp_path.name}".replace("-", "_")
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for module_name, source in files.items():
        (pkg_dir / f"{module_name}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    package = importlib.import_module(name)
    monkeypatch.setitem(sys.modules, name, package)
    return package


def _with_ops(op_id: str) -> str:
    return _ROW.format(op_id=op_id) + "\nOPS = (ROW,)\n"


def test_collect_ops_takes_rows_from_every_ops_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _fake_package(
        tmp_path,
        monkeypatch,
        {
            "ops_zeta": _with_ops("x.b"),
            "ops_alpha": _with_ops("x.c"),
            "ops_helper": "HELPER = 1\n",
            "ops_beta": _with_ops("x.a"),
            "other": _with_ops("x.not_an_ops_module"),
        },
    )
    assert [op.op_id for op in collect_ops(package)] == ["x.a", "x.b", "x.c"]


def test_collect_ops_rejects_an_op_id_two_modules_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _fake_package(
        tmp_path, monkeypatch, {"ops_one": _with_ops("x.dup"), "ops_two": _with_ops("x.dup")}
    )
    with pytest.raises(ValueError, match=r"x\.dup"):
        collect_ops(package)


def test_collect_ops_rejects_a_row_that_is_not_a_kubernetes_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _fake_package(tmp_path, monkeypatch, {"ops_dict_rows": 'OPS = ({"op_id": "x.a"},)\n'})
    with pytest.raises(TypeError, match="ops_dict_rows"):
        collect_ops(package)


def test_guard_reports_a_module_whose_rows_are_not_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module that holds rows but does not export them as ``OPS`` is reported."""
    package = _fake_package(
        tmp_path,
        monkeypatch,
        {
            "ops_good": _with_ops("x.good"),
            "ops_forgot": _ROW.format(op_id="x.forgot") + "\nFORGOT_OPS = (ROW,)\n",
            "not_ops_prefix": _with_ops("x.wrong_name"),
        },
    )
    modules = [
        importlib.import_module(f"{package.__name__}.{name}")
        for name in ("ops_good", "ops_forgot", "not_ops_prefix")
    ]
    assert _uncollected_rows(modules, collect_ops(package)) == [
        f"{package.__name__}.not_ops_prefix: x.wrong_name",
        f"{package.__name__}.ops_forgot: x.forgot",
    ]
