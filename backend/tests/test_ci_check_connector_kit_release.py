# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/release/check_connector_kit_release.py``.

The release check compares the connector kit's name list at the last
release tag with the list now. When the kit changed, the release notes need
a ``### Connector kit`` section that names the new kit version, and the kit
version must have gone up far enough. The tests run the script as a
subprocess, with ``--old-names`` in place of git.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


_SCRIPT = _repo_root() / "scripts" / "release" / "check_connector_kit_release.py"

_ENTRY = {"kind": "function", "inputs": ["value: str"], "returns": "str"}
_CHANGED = {"kind": "function", "inputs": ["value: str", "*", "limit: int"], "returns": "str"}
_NAMED = {"kind": "function", "inputs": ["value: str", "*", "size: int = 1"], "returns": "str"}
_NAMED_PLUS_DEFAULT = {
    "kind": "function",
    "inputs": ["value: str", "*", "size: int = 1", "limit: int = 10"],
    "returns": "str",
}
_NAMED_PLUS_REQUIRED = {
    "kind": "function",
    "inputs": ["value: str", "*", "size: int = 1", "limit: int"],
    "returns": "str",
}


def _names(version: str, **entries: dict[str, Any]) -> dict[str, Any]:
    return {"kit_version": version, "names": entries}


def _changelog(kit_section: str | None) -> str:
    lines = ["# Changelog", "", "## [Unreleased]", "", "## [0.50.0] - 2026-11-20", ""]
    lines += ["### Added", "", "- Something new (#1).", ""]
    if kit_section is not None:
        lines += ["### Connector kit", "", kit_section, ""]
    lines += ["## [0.49.0] - 2026-11-10", "", "### Connector kit", "", "- Kit version 9.9.", ""]
    return "\n".join(lines)


def _run(
    tmp_path: Path,
    old: dict[str, Any],
    new: dict[str, Any],
    kit_section: str | None,
) -> subprocess.CompletedProcess[str]:
    old_file = tmp_path / "old.json"
    new_file = tmp_path / "new.json"
    changelog = tmp_path / "CHANGELOG.md"
    old_file.write_text(json.dumps(old))
    new_file.write_text(json.dumps(new))
    changelog.write_text(_changelog(kit_section))
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--version",
            "0.50.0",
            "--old-names",
            str(old_file),
            "--new-names",
            str(new_file),
            "--changelog",
            str(changelog),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_unchanged_kit_needs_no_section(tmp_path: Path) -> None:
    kit = _names("1.0", a=_ENTRY)
    result = _run(tmp_path, kit, kit, None)
    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout


def test_added_name_with_section_and_new_version_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.1", a=_ENTRY, b=_ENTRY),
        "- Kit version 1.1 adds `b`.",
    )
    assert result.returncode == 0, result.stderr


def test_changed_kit_without_section_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, _names("1.0", a=_ENTRY), _names("1.1", a=_ENTRY, b=_ENTRY), None)
    assert result.returncode == 1
    assert "### Connector kit" in result.stderr
    assert "1.1" in result.stderr


def test_section_naming_another_version_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.1", a=_ENTRY, b=_ENTRY),
        "- Kit version 1.10 adds `b`.",
    )
    assert result.returncode == 1
    assert "does not name the new kit version 1.1" in result.stderr


def test_changed_name_needs_the_first_number(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.1", a=_CHANGED),
        "- Kit version 1.1 changes `a`.",
    )
    assert result.returncode == 1
    assert "first number" in result.stderr


def test_new_optional_named_input_needs_only_the_second_number(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_NAMED),
        _names("1.1", a=_NAMED_PLUS_DEFAULT),
        "- Kit version 1.1 gives `a` the input `limit`.",
    )
    assert result.returncode == 0, result.stderr


def test_new_optional_named_input_still_needs_a_new_version(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_NAMED),
        _names("1.0", a=_NAMED_PLUS_DEFAULT),
        "- Kit version 1.0 gives `a` the input `limit`.",
    )
    assert result.returncode == 1
    assert "must go up" in result.stderr


def test_new_named_input_without_a_default_needs_the_first_number(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_NAMED),
        _names("1.1", a=_NAMED_PLUS_REQUIRED),
        "- Kit version 1.1 gives `a` the input `limit`.",
    )
    assert result.returncode == 1
    assert "first number" in result.stderr


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("check_connector_kit_release", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_KIT_PROBLEMS = _load_script().kit_problems


def _fn(*inputs: str, returns: str = "str") -> dict[str, Any]:
    return {"kind": "function", "inputs": list(inputs), "returns": returns}


# The cases of the rule for new inputs. The same cases run for the writer in
# tests/test_connector_kit.py.
_F_NAMED = _fn("value: str", "*", "size: int = 1")
_RULE_CASES = [
    pytest.param(
        _fn("value: str"), _fn("value: str", "*", "opt: int = 1"), "second", id="no-star-before"
    ),
    pytest.param(
        _fn("value: str", "*items: str"),
        _fn("value: str", "*items: str", "opt: int = 1"),
        "second",
        id="named-after-args",
    ),
    pytest.param(
        _F_NAMED,
        _fn("value: str", "*", "size: int = 1", "limit: int = 10"),
        "second",
        id="named-with-default",
    ),
    pytest.param(
        _F_NAMED,
        _fn("value: str", "*", "size: int = 1", "sep: str = ' = '"),
        "second",
        id="eq-in-default",
    ),
    pytest.param(
        _F_NAMED,
        _fn("value: str", "*", "size: int = 1", "expr: Literal['a = b']"),
        "first",
        id="eq-in-type",
    ),
    pytest.param(
        _F_NAMED,
        _fn("value: str", "*", "size: int = 1", "limit: int"),
        "first",
        id="named-without-default",
    ),
    pytest.param(
        _F_NAMED,
        _fn("value: str", "limit: int = 10", "*", "size: int = 1"),
        "first",
        id="by-place-with-default",
    ),
    pytest.param(_F_NAMED, _fn("value: str", "*", "size: int = 2"), "first", id="changed-default"),
    pytest.param(_F_NAMED, _fn("value: str", "*", "size: float = 1"), "first", id="changed-type"),
    pytest.param(
        _F_NAMED,
        _fn("value: str", "*", "size: int = 1", "limit: int = 10", returns="int"),
        "first",
        id="plus-new-return",
    ),
    pytest.param(
        _fn("value: str", "*", "size: int = 1", "**options: Any"),
        _fn("value: str", "*", "size: int = 1", "mode: int = 0", "**options: Any"),
        "first",
        id="next-to-kwargs",
    ),
    pytest.param(_F_NAMED, _fn("value: str"), "first", id="last-named-removed"),
]


@pytest.mark.parametrize(("before", "after", "level"), _RULE_CASES)
def test_the_rule_for_new_inputs(before: dict[str, Any], after: dict[str, Any], level: str) -> None:
    old = _names("1.0", f=before)
    assert _KIT_PROBLEMS(old, _names("2.0", f=after)) == []
    second = _KIT_PROBLEMS(old, _names("1.1", f=after))
    if level == "second":
        assert second == []
    else:
        assert len(second) == 1
        assert "first number" in second[0]


def test_removing_the_last_named_input_with_the_first_number_up_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_NAMED),
        _names("2.0", a=_ENTRY),
        "- Kit version 2.0: `a` no longer takes `size`. Leave `size` out.",
    )
    assert result.returncode == 0, result.stderr


def test_a_name_both_extended_and_marked_as_old_is_named_once() -> None:
    marked = {**_NAMED_PLUS_DEFAULT, "old": "Use b."}
    problems = _KIT_PROBLEMS(_names("1.0", widget_list=_NAMED), _names("1.0", widget_list=marked))
    assert len(problems) == 1
    assert problems[0].count("widget_list") == 1


def test_added_name_needs_a_new_version(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY),
        _names("1.0", a=_ENTRY, b=_ENTRY),
        "- Kit version 1.0 adds `b`.",
    )
    assert result.returncode == 1
    assert "must go up" in result.stderr


def test_removing_a_name_that_was_not_old_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.0", a=_ENTRY, b=_ENTRY),
        _names("2.0", a=_ENTRY),
        "- Kit version 2.0 removes `b`.",
    )
    assert result.returncode == 1
    assert "did not mark them as old: b" in result.stderr


def test_removing_an_old_name_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _names("1.1", a=_ENTRY, b={**_ENTRY, "old": "use a"}),
        _names("2.0", a=_ENTRY),
        "- Kit version 2.0 removes `b`: use `a`.",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", ["old", "changelog"])
def test_unreadable_input_exits_2(tmp_path: Path, missing: str) -> None:
    kit = tmp_path / "kit.json"
    kit.write_text(json.dumps(_names("1.0", a=_ENTRY)))
    args = ["--old-names", str(kit), "--new-names", str(kit)]
    args += ["--changelog", str(tmp_path / "CHANGELOG.md")]
    if missing == "old":
        args[1] = str(tmp_path / "missing.json")
    else:
        (tmp_path / "CHANGELOG.md").unlink(missing_ok=True)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--version", "0.50.0", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
