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
            lambda t: t["outputSchema"]["properties"]["items"]["items"]["properties"].pop("id"),
            "MCP tool list_things output.items[]: field 'id' removed",
        ),
        (
            lambda t: t.update(outputSchema=None),
            "MCP tool list_things: output schema removed",
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
        lambda t: t["inputSchema"]["properties"]["status"].pop("enum"),
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
