# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/ci/check_contract_compat.py``.

The script fails CI when a pull request breaks the REST API snapshot
(``cli/api/openapi.json``) or the MCP tools snapshot
(``backend/contracts/mcp-tools-list.json``), unless the pull request has
the ``breaking-change`` label and a ``## Breaking changes`` section. The
MCP rules are tested as functions. The whole script runs as a subprocess
against small throw-away git repositories, with a stand-in for the
``oasdiff`` binary.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_contract_compat.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_contract_compat", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compat = _load()


def _tool(name: str = "list_things", surface: str = "working", **extra: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": name,
        "surface": surface,
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "status": {"type": "string", "enum": ["open", "closed"]},
                "filter": {"type": "object", "properties": {"kind": {"type": "string"}}},
                "value": {},
            },
            "required": [],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "string"}}},
                },
                "next_cursor": {"type": ["string", "null"]},
            },
            "required": ["items", "next_cursor"],
        },
    }
    tool.update(extra)
    return tool


def _snap(*tools: dict[str, Any]) -> dict[str, Any]:
    return {"tools": list(tools)}


def _changed(edit: Any) -> list[str]:
    base = _tool()
    head = copy.deepcopy(base)
    edit(head)
    return compat.mcp_breaking(_snap(base), _snap(head))


# ---------------------------------------------------------------------------
# MCP rules
# ---------------------------------------------------------------------------


def test_same_snapshot_has_no_breaking_change() -> None:
    assert compat.mcp_breaking(_snap(_tool()), _snap(_tool())) == []


def test_removed_tool_is_breaking() -> None:
    assert compat.mcp_breaking(_snap(_tool(), _tool("old")), _snap(_tool())) == [
        "MCP tool old: removed"
    ]


def test_new_tool_is_not_breaking() -> None:
    assert compat.mcp_breaking(_snap(_tool()), _snap(_tool(), _tool("new"))) == []


def test_move_off_the_working_surface_is_breaking_but_the_reverse_is_not() -> None:
    assert compat.mcp_breaking(_snap(_tool()), _snap(_tool(surface="operator"))) == [
        "MCP tool list_things: moved off the working surface to 'operator'"
    ]
    assert compat.mcp_breaking(_snap(_tool(surface="operator")), _snap(_tool())) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            lambda t: t["inputSchema"]["properties"].pop("limit"),
            "MCP tool list_things input: argument 'limit' removed",
        ),
        (
            lambda t: t["inputSchema"]["required"].append("limit"),
            "MCP tool list_things input: argument 'limit' is now required",
        ),
        (
            lambda t: t["inputSchema"]["properties"]["status"]["enum"].remove("closed"),
            "MCP tool list_things input.status: enum value 'closed' removed",
        ),
        (
            lambda t: t["inputSchema"]["properties"]["limit"].update(type="string"),
            "MCP tool list_things input.limit: type changed from ['integer'] to ['string']",
        ),
        (
            lambda t: t["inputSchema"]["properties"]["filter"]["properties"].pop("kind"),
            "MCP tool list_things input.filter: argument 'kind' removed",
        ),
        (
            lambda t: t["outputSchema"]["properties"].pop("next_cursor"),
            "MCP tool list_things output: field 'next_cursor' removed",
        ),
        (
            lambda t: t["outputSchema"]["required"].remove("items"),
            "MCP tool list_things output: field 'items' is no longer always returned",
        ),
        (
            lambda t: t["outputSchema"]["properties"]["next_cursor"].update(type="string"),
            "MCP tool list_things output.next_cursor: "
            "type changed from ['null', 'string'] to ['string']",
        ),
        (
            lambda t: t["outputSchema"]["properties"]["items"]["items"]["properties"]["id"].update(
                type="number"
            ),
            "MCP tool list_things output.items[].id: type changed from ['string'] to ['number']",
        ),
        (
            lambda t: t["outputSchema"]["properties"]["items"]["items"]["properties"].pop("id"),
            "MCP tool list_things output.items[]: field 'id' removed",
        ),
        (
            lambda t: t.update(outputSchema=None),
            "MCP tool list_things: output schema removed",
        ),
        (
            lambda t: t["inputSchema"]["properties"]["value"].update(type="string"),
            "MCP tool list_things input.value: type changed from any to ['string']",
        ),
        (
            lambda t: t["outputSchema"]["properties"]["next_cursor"].pop("type"),
            "MCP tool list_things output.next_cursor: type changed from ['null', 'string'] to any",
        ),
    ],
)
def test_breaking_schema_changes_are_found(edit: Any, expected: str) -> None:
    assert _changed(edit) == [expected]


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t["inputSchema"]["properties"].update(new={"type": "string"}),
        lambda t: t["inputSchema"]["properties"]["status"]["enum"].append("archived"),
        lambda t: t["inputSchema"]["properties"]["limit"].update(type=["integer", "null"]),
        lambda t: t["inputSchema"]["properties"]["limit"].update(type="number"),
        lambda t: t["inputSchema"]["properties"]["status"].pop("enum"),
        lambda t: t["inputSchema"]["properties"]["limit"].pop("type"),
        lambda t: t["outputSchema"]["properties"].update(extra={"type": "string"}),
        lambda t: t["outputSchema"]["required"].append("extra"),
        lambda t: t["outputSchema"]["properties"]["items"]["items"]["properties"].update(
            name={"type": "string"}
        ),
    ],
)
def test_additive_changes_are_not_breaking(edit: Any) -> None:
    assert _changed(edit) == []


def test_new_output_schema_is_not_breaking() -> None:
    base = _tool(outputSchema=None)
    assert compat.mcp_breaking(_snap(base), _snap(_tool())) == []


def test_new_required_argument_on_a_new_property_is_breaking() -> None:
    def edit(t: dict[str, Any]) -> None:
        t["inputSchema"]["properties"]["target"] = {"type": "string"}
        t["inputSchema"]["required"].append("target")

    assert _changed(edit) == ["MCP tool list_things input: argument 'target' is now required"]


def _branched() -> dict[str, Any]:
    """A tool whose input and output have ``anyOf`` / ``oneOf`` branches."""
    return _tool(
        name="search",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "target": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            },
        },
        outputSchema={
            "type": "object",
            "oneOf": [
                {
                    "properties": {"hits": {"type": "array"}, "took_ms": {"type": "integer"}},
                    "required": ["hits", "took_ms"],
                },
                {"properties": {"operation": {"type": "object"}}, "required": ["operation"]},
            ],
        },
    )


def _branched_changed(edit: Any) -> list[str]:
    base = _branched()
    head = copy.deepcopy(base)
    edit(head)
    return compat.mcp_breaking(_snap(base), _snap(head))


def _drop_hits(t: dict[str, Any]) -> None:
    branch = t["outputSchema"]["oneOf"][0]
    branch["properties"].pop("hits")
    branch["required"].remove("hits")


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            _drop_hits,
            [
                "MCP tool search output oneOf[0]: field 'hits' is no longer always returned",
                "MCP tool search output oneOf[0]: field 'hits' removed",
            ],
        ),
        (
            lambda t: t["outputSchema"]["oneOf"][1]["required"].remove("operation"),
            ["MCP tool search output oneOf[1]: field 'operation' is no longer always returned"],
        ),
        (
            lambda t: t["outputSchema"]["oneOf"][0]["properties"]["took_ms"].update(type="string"),
            [
                "MCP tool search output oneOf[0].took_ms: "
                "type changed from ['integer'] to ['string']"
            ],
        ),
        (
            lambda t: t["outputSchema"]["oneOf"].append({"properties": {}}),
            ["MCP tool search output: oneOf has 3 branches instead of 2"],
        ),
        (
            lambda t: t["outputSchema"].pop("oneOf"),
            ["MCP tool search output: oneOf removed"],
        ),
        (
            lambda t: t["inputSchema"]["properties"]["query"]["anyOf"].pop(),
            ["MCP tool search input.query: anyOf has 1 branches instead of 2"],
        ),
        (
            lambda t: t["inputSchema"]["properties"]["query"]["anyOf"][0].update(type="boolean"),
            ["MCP tool search input.query anyOf[0]: type changed from ['string'] to ['boolean']"],
        ),
        (
            lambda t: t["inputSchema"]["properties"].update(mode={"oneOf": [{"type": "string"}]}),
            [],
        ),
    ],
)
def test_branch_changes(edit: Any, expected: list[str]) -> None:
    assert _branched_changed(edit) == expected


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t["outputSchema"]["oneOf"][0]["properties"].update(extra={"type": "string"}),
        lambda t: t["outputSchema"]["oneOf"].pop(),
        lambda t: t["inputSchema"]["properties"]["query"]["anyOf"].append({"type": "null"}),
        lambda t: t["inputSchema"]["properties"]["query"]["anyOf"].append({"type": "number"}),
        lambda t: t["inputSchema"]["properties"]["query"].pop("anyOf"),
        lambda t: t["inputSchema"]["properties"]["target"]["oneOf"].append({"type": "null"}),
        lambda t: t["inputSchema"]["properties"]["target"]["oneOf"].append({"type": "boolean"}),
        lambda t: t["inputSchema"]["properties"]["target"]["oneOf"][1].update(type="number"),
    ],
)
def test_branch_changes_that_are_not_breaking(edit: Any) -> None:
    assert _branched_changed(edit) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            lambda t: t["inputSchema"]["properties"]["target"]["oneOf"].append({"type": "number"}),
            "MCP tool search input.target: oneOf branches 1 and 2 both accept ['integer'] now "
            "(a value that fits two branches fails oneOf)",
        ),
        (
            lambda t: t["inputSchema"]["properties"]["target"]["oneOf"].append({}),
            "MCP tool search input.target: oneOf branches 0 and 2 both accept ['string'] now "
            "(a value that fits two branches fails oneOf)",
        ),
        (
            lambda t: t["inputSchema"]["properties"]["target"]["oneOf"][1].pop("type"),
            "MCP tool search input.target: oneOf branches 0 and 1 both accept ['string'] now "
            "(a value that fits two branches fails oneOf)",
        ),
    ],
)
def test_input_one_of_branches_that_start_to_overlap_are_breaking(edit: Any, expected: str) -> None:
    found = _branched_changed(edit)
    assert expected in found
    assert all("both accept" in line for line in found)


def test_input_one_of_branches_that_overlapped_before_are_not_reported() -> None:
    base = _branched()
    base["inputSchema"]["properties"]["target"]["oneOf"] = [
        {"type": "object", "required": ["a"]},
        {"type": "object", "required": ["b"]},
    ]
    head = copy.deepcopy(base)
    head["inputSchema"]["properties"]["target"]["oneOf"][0]["properties"] = {"a": {}}
    assert compat.mcp_breaking(_snap(base), _snap(head)) == []


def _one_of_changed(old: list[Any], new: list[Any]) -> list[str]:
    """Return the breaking changes of the ``target`` input ``oneOf`` going from *old* to *new*."""
    base = _branched()
    base["inputSchema"]["properties"]["target"]["oneOf"] = old
    head = copy.deepcopy(base)
    head["inputSchema"]["properties"]["target"]["oneOf"] = new
    return compat.mcp_breaking(_snap(base), _snap(head))


def test_an_old_overlap_does_not_hide_a_new_one() -> None:
    """Both branches took ``null`` before; now both take a string too."""
    found = _one_of_changed(
        [{"type": ["string", "null"]}, {"type": ["integer", "null"]}],
        [{"type": ["string", "null"]}, {"type": ["integer", "null", "string"]}],
    )
    assert found == [
        "MCP tool search input.target: oneOf branches 0 and 1 both accept ['string'] now "
        "(a value that fits two branches fails oneOf)"
    ]


_A, _B, _C = ({"type": "string", "enum": [v]} for v in ("a", "b", "c"))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A new enum branch with its own values cannot overlap the others.
        ([_A, _B], [_A, _B, _C]),
        # Enum values of another type than the plain branch do not overlap it.
        ([{"type": "string"}], [{"type": "string"}, {"enum": [1, None]}]),
        # Both branches took every string before; a value they both list is not new.
        ([{"type": "string"}, {}], [_A, {"enum": ["a", 2]}]),
    ],
)
def test_one_of_enum_branches_that_cannot_overlap_are_not_breaking(
    old: list[Any], new: list[Any]
) -> None:
    assert _one_of_changed(old, new) == []


@pytest.mark.parametrize(
    ("old", "new", "accepted"),
    [
        # A new enum branch lists a value another enum branch lists.
        ([_A, _B], [_A, _B, {"type": "string", "enum": ["b", "c"]}], 'the values ["b"]'),
        # A new enum branch holds a string, and a plain branch takes every string.
        (
            [{"type": "string"}, {"type": "integer"}],
            [{"type": "string"}, {"type": "integer"}, _C],
            'the values ["c"]',
        ),
        # An enum branch loses its enum and takes every string now, also the other branch's "a".
        ([_A, _B], [_A, {"type": "string"}], 'the values ["a"]'),
        # True is a boolean, not the integer 1.
        ([{"enum": [1]}, {"enum": [True]}], [{"enum": [1]}, {"enum": [True, 1]}], "the values [1]"),
    ],
)
def test_one_of_enum_branches_that_start_to_overlap_are_breaking(
    old: list[Any], new: list[Any], accepted: str
) -> None:
    found = _one_of_changed(old, new)
    assert len(found) == 1
    assert f"both accept {accepted} now" in found[0]


@pytest.mark.parametrize(
    ("old", "new_branch"),
    [
        # In JSON Schema 1.0 equals 1, so the input 1 fits both enum branches.
        ([{"enum": [1]}], {"enum": [1.0]}),
        # 1 is an integer, and the integer branch takes every integer.
        ([{"type": "integer"}], {"enum": [1]}),
        # 1.0 is an integer too.
        ([{"type": "integer"}], {"type": "number", "enum": [1.0]}),
    ],
)
def test_one_of_number_enums_compare_the_json_schema_way(
    old: list[Any], new_branch: dict[str, Any]
) -> None:
    found = _one_of_changed(old, [*old, new_branch])
    assert len(found) == 1
    assert "both accept the values [1] now" in found[0]


@pytest.mark.parametrize(
    "new_branch",
    [
        # True is a boolean, not the number 1.
        {"enum": [True]},
        # The string "1" is not the number 1.
        {"enum": ["1"]},
        # 1.5 is not an integer.
        {"type": "number", "enum": [1.5]},
    ],
)
def test_one_of_enums_of_another_json_type_do_not_overlap_a_number(
    new_branch: dict[str, Any],
) -> None:
    for old in ({"enum": [1]}, {"type": "integer"}):
        assert _one_of_changed([old], [old, new_branch]) == []


def test_enum_value_written_as_float_is_not_removed() -> None:
    """``1`` becoming ``1.0`` is the same value; ``true`` replacing ``1`` is a removal."""

    def same(t: dict[str, Any]) -> None:
        t["inputSchema"]["properties"]["limit"] = {"type": "number", "enum": [1.0]}

    def other(t: dict[str, Any]) -> None:
        t["inputSchema"]["properties"]["limit"] = {"enum": [True]}

    base = _tool()
    base["inputSchema"]["properties"]["limit"] = {"type": "number", "enum": [1]}
    for edit, expected in (
        (same, []),
        (other, ["MCP tool list_things input.limit: enum value 1 removed"]),
    ):
        head = copy.deepcopy(base)
        edit(head)
        assert compat.mcp_breaking(_snap(base), _snap(head)) == expected


def _reverse_branches(t: dict[str, Any]) -> None:
    t["outputSchema"]["oneOf"].reverse()
    t["inputSchema"]["properties"]["query"]["anyOf"].reverse()
    t["inputSchema"]["properties"]["target"]["oneOf"].reverse()


def test_reordered_branches_are_not_breaking() -> None:
    assert _branched_changed(_reverse_branches) == []


def test_reordered_branches_are_still_compared_with_their_partner() -> None:
    def edit(t: dict[str, Any]) -> None:
        _reverse_branches(t)
        t["outputSchema"]["oneOf"][1]["properties"]["took_ms"].update(type="string")
        t["inputSchema"]["properties"]["query"]["anyOf"][0].update(type="boolean")

    assert _branched_changed(edit) == [
        "MCP tool search input.query anyOf[0]: type changed from ['integer'] to ['boolean']",
        "MCP tool search output oneOf[1].took_ms: type changed from ['integer'] to ['string']",
    ]


def test_new_branches_on_an_input_are_breaking() -> None:
    def edit(t: dict[str, Any]) -> None:
        t["inputSchema"]["properties"]["filter"]["oneOf"] = [{"required": ["kind"]}]

    assert _changed(edit) == ["MCP tool list_things input.filter: oneOf added"]


def test_committed_snapshot_has_no_break_against_itself() -> None:
    snapshot = json.loads((_REPO_ROOT / compat.MCP_PATH).read_text(encoding="utf-8"))
    assert snapshot["tools"]
    assert compat.mcp_breaking(snapshot, snapshot) == []


# ---------------------------------------------------------------------------
# Label and pull request text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("## Breaking changes\n\n- `list_things` lost `limit`; use `page_size`.\n", True),
        ("### breaking changes\nClients must send `target`.", True),
        ("## Breaking changes\n\n## Test plan\n- ran it", False),
        ("## Breaking changes\n<!-- write here -->\n", False),
        ("## Summary\nNo breaking changes here.", False),
        ("", False),
    ],
)
def test_breaking_section_needs_a_heading_and_text(body: str, expected: bool) -> None:
    assert compat.has_breaking_section(body) is expected


def test_allowed_needs_both_label_and_section() -> None:
    body = "## Breaking changes\n- send `target` now\n"
    assert compat.allowed(["breaking-change"], body) == (True, "")
    ok, missing = compat.allowed(["docs"], body)
    assert not ok and "label" in missing and "section" not in missing
    ok, missing = compat.allowed(["breaking-change"], "## Summary\n")
    assert not ok and "section" in missing and "label" not in missing
    ok, missing = compat.allowed([], None)
    assert not ok and "label" in missing and "section" in missing


# ---------------------------------------------------------------------------
# The whole script against throw-away git repositories
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _fake_oasdiff(tmp_path: Path, exit_code: int, output: str = "") -> Path:
    """A stand-in for oasdiff that records its arguments and exits as told."""
    script = tmp_path / "fake-oasdiff"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"pathlib.Path({str(tmp_path / 'args.txt')!r}).write_text(' '.join(sys.argv[1:]))\n"
        f"sys.stdout.write({output!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def _repo(tmp_path: Path, base_mcp: dict[str, Any] | None, with_openapi: bool = True) -> Path:
    root = tmp_path / "repo"
    (root / "cli" / "api").mkdir(parents=True)
    (root / "backend" / "contracts").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    if with_openapi:
        (root / compat.OPENAPI_PATH).write_text('{"openapi": "3.0.3", "paths": {}}')
    if base_mcp is not None:
        (root / compat.MCP_PATH).write_text(json.dumps(base_mcp))
    (root / "README").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run(root: Path, oasdiff: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(root),
            "--base",
            "HEAD",
            "--oasdiff",
            str(oasdiff),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_passes_when_nothing_breaks(tmp_path: Path) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    result = _run(root, _fake_oasdiff(tmp_path, 0))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No breaking change" in result.stdout
    args = (tmp_path / "args.txt").read_text()
    assert "breaking --fail-on ERR --match-path ^/api/v1/" in args
    assert args.endswith(str(root / compat.OPENAPI_PATH))


def test_script_fails_on_an_mcp_break_without_label(tmp_path: Path) -> None:
    root = _repo(tmp_path, _snap(_tool(), _tool("old")))
    (root / compat.MCP_PATH).write_text(json.dumps(_snap(_tool())))
    result = _run(root, _fake_oasdiff(tmp_path, 0), "--labels", "docs")
    assert result.returncode == 1
    assert "MCP tool old: removed" in result.stdout
    assert "breaking-change" in result.stdout


def test_script_reports_oasdiff_errors_and_allows_them_with_label_and_section(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    output = (
        "2 changes: 1 error, 1 warning, 0 info\n"
        "error, in API GET /api/v1/things removed the success response property 'id' "
        "[response-property-removed].\n\n"
        "warning, in API GET /api/v1/things something maybe [x].\n\n"
    )
    oasdiff = _fake_oasdiff(tmp_path, 1, output)
    blocked = _run(root, oasdiff)
    assert blocked.returncode == 1
    assert "REST API: error, in API GET /api/v1/things" in blocked.stdout
    assert "warning, in API" not in blocked.stdout

    body = tmp_path / "body.md"
    body.write_text("## Breaking changes\n- read `uuid` instead of `id`.\n")
    allowed = _run(root, oasdiff, "--labels", "breaking-change, docs", "--pr-body-file", str(body))
    assert allowed.returncode == 0, allowed.stdout
    assert "Allowed" in allowed.stdout


def test_script_skips_a_snapshot_that_is_not_on_the_base_yet(tmp_path: Path) -> None:
    root = _repo(tmp_path, None, with_openapi=False)
    (root / compat.MCP_PATH).write_text(json.dumps(_snap(_tool())))
    result = _run(root, _fake_oasdiff(tmp_path, 1, "error, should not run\n"))
    assert result.returncode == 0, result.stdout
    assert "REST API check skipped" in result.stdout
    assert "MCP tools check skipped" in result.stdout
    assert not (tmp_path / "args.txt").exists()


@pytest.mark.parametrize("exit_code", [2, 102])
def test_script_fails_closed_when_oasdiff_cannot_run(tmp_path: Path, exit_code: int) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    result = _run(root, _fake_oasdiff(tmp_path, exit_code, "load failed"))
    assert result.returncode == 2
    assert "could not run" in result.stdout


def test_script_fails_closed_without_oasdiff(tmp_path: Path) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    result = _run(root, tmp_path / "no-such-binary")
    assert result.returncode == 2
    assert "cannot run oasdiff" in result.stdout


def test_script_fails_closed_on_an_unknown_base(tmp_path: Path) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--root",
            str(root),
            "--base",
            "v9.9.9",
            "--oasdiff",
            "oasdiff",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "does not exist" in result.stdout


def test_script_fails_closed_on_a_broken_snapshot(tmp_path: Path) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    (root / compat.MCP_PATH).write_text("{not json")
    result = _run(root, _fake_oasdiff(tmp_path, 0))
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# Helm values
# ---------------------------------------------------------------------------

_VALUES = "image:\n  tag: x\n  pullPolicy: IfNotPresent\ningress:\n  host: ''\nitems:\n  - a\n"


def test_values_a_removed_key_is_breaking() -> None:
    head = "image:\n  tag: x\ningress:\n  host: ''\nitems:\n  - a\n"
    assert compat.values_breaking(_VALUES, head) == ["Helm value 'image.pullPolicy' removed"]


def test_values_a_removed_section_lists_the_section_and_its_keys() -> None:
    head = "image:\n  tag: x\n  pullPolicy: IfNotPresent\nitems:\n  - a\n"
    assert compat.values_breaking(_VALUES, head) == [
        "Helm value 'ingress' removed",
        "Helm value 'ingress.host' removed",
    ]


def test_values_new_keys_a_changed_default_and_a_changed_list_are_not_breaking() -> None:
    head = (
        "image:\n  tag: y\n  pullPolicy: Always\n  extra: 1\n"
        "ingress:\n  host: example.invalid\nitems:\n  - b\n  - c\nnew: {}\n"
    )
    assert compat.values_breaking(_VALUES, head) == []


def test_values_an_empty_file_is_not_a_crash() -> None:
    assert compat.values_breaking("", "") == []
    assert compat.values_breaking(_VALUES, "") != []


def test_values_schema_uses_the_input_rules() -> None:
    base = {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["a", "b"]}, "gone": {"type": "string"}},
    }
    head = {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["a"]}},
        "required": ["mode"],
    }
    found = compat.values_schema_breaking(base, head)
    assert any("'gone' removed" in line for line in found)
    assert any("'mode' is now required" in line for line in found)
    assert any("enum" in line for line in found)
    assert compat.values_schema_breaking(head, base) == []


def _add_chart_files(root: Path, values: str, schema: dict[str, Any]) -> None:
    (root / "deploy" / "charts" / "meho").mkdir(parents=True, exist_ok=True)
    (root / compat.VALUES_PATH).write_text(values)
    (root / compat.VALUES_SCHEMA_PATH).write_text(json.dumps(schema))


def test_script_fails_on_a_removed_helm_value_and_allows_it_with_label_and_section(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    _add_chart_files(root, "a: 1\nb: 2\n", schema)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chart")
    (root / compat.VALUES_PATH).write_text("a: 1\n")
    oasdiff = _fake_oasdiff(tmp_path, 0)
    blocked = _run(root, oasdiff)
    assert blocked.returncode == 1
    assert "Helm value 'b' removed" in blocked.stdout

    body = tmp_path / "body.md"
    body.write_text("## Breaking changes\n- stop setting `b`.\n")
    allowed = _run(root, oasdiff, "--labels", "breaking-change", "--pr-body-file", str(body))
    assert allowed.returncode == 0, allowed.stdout


def test_script_fails_closed_on_broken_helm_values(tmp_path: Path) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    _add_chart_files(root, "a: 1\n", {"type": "object"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chart")
    (root / compat.VALUES_PATH).write_text("a: [unclosed\n")
    result = _run(root, _fake_oasdiff(tmp_path, 0))
    assert result.returncode == 2
    assert "could not run" in result.stdout


def test_script_skips_helm_files_that_are_not_on_the_base_yet(tmp_path: Path) -> None:
    root = _repo(tmp_path, _snap(_tool()))
    _add_chart_files(root, "a: 1\n", {"type": "object"})
    result = _run(root, _fake_oasdiff(tmp_path, 0))
    assert result.returncode == 0, result.stdout
    assert "Helm values check skipped" in result.stdout
    assert "Helm schema check skipped" in result.stdout
