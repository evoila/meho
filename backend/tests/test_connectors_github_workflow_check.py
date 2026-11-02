# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the GitHub workflow check in the probe of a repository target.

A workflow that starts on an issue, comment, sub-issue or board event would
be started by a MEHO ticket write. The probe of a target that names one
repository reads ``.github/workflows/`` and warns about each such workflow.
A failed read is reported as ``unknown``, never as "no workflows".

All repository names are made up; no live GitHub.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.connector import GitHubRestConnector
from meho_backplane.connectors.github.session import (
    DEFAULT_GITHUB_API_URL,
    GitHubPATCredentials,
    GitHubTargetLike,
)
from meho_backplane.connectors.github.workflow_check import (
    MAX_WORKFLOW_FILES,
    ticket_event_workflow_check,
    workflow_events,
)
from meho_backplane.connectors.schemas import AuthModel

_REPO = "acme/widgets"
_DIR = f"/repos/{_REPO}/contents/.github/workflows"

_ON_ISSUES = """
name: triage
on:
  issues:
    types: [opened, labeled]
jobs:
  run:
    runs-on: ubuntu-latest
    steps: []
"""
_ON_PUSH = "name: ci\non: [push, pull_request]\njobs: {}\n"
_ON_COMMENT = "name: bot\non: issue_comment\njobs: {}\n"


def _operator() -> Operator:
    return Operator(
        sub="op-gh-workflows",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _file(text: str) -> dict[str, Any]:
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def _entry(name: str, kind: str = "file") -> dict[str, Any]:
    return {"type": kind, "path": f".github/workflows/{name}", "name": name}


class _Session:
    """Answers ``_get_json`` from a path map; a value may be an exception."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self._answers = answers
        self.paths: list[str] = []

    async def _get_json(self, target: Any, path: str, *, operator: Operator) -> Any:
        self.paths.append(path)
        value = self._answers[path]
        if isinstance(value, Exception):
            raise value
        return value


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/x")
    return httpx.HTTPStatusError(
        f"http_{code}", request=request, response=httpx.Response(code, request=request)
    )


async def _check(answers: dict[str, Any]) -> dict[str, Any]:
    return await ticket_event_workflow_check(
        _Session(answers),  # type: ignore[arg-type]
        object(),
        _operator(),
        _REPO,
    )


# ---------------------------------------------------------------------------
# workflow_events: the ``on:`` key in its three forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_ON_ISSUES, {"issues"}),
        (_ON_PUSH, {"push", "pull_request"}),
        (_ON_COMMENT, {"issue_comment"}),
        ('"on":\n  projects_v2_item: {}\n', {"projects_v2_item"}),
        ("name: no trigger\njobs: {}\n", set()),
    ],
)
def test_workflow_events_reads_each_form_of_on(text: str, expected: set[str]) -> None:
    assert workflow_events(text) == expected


@pytest.mark.parametrize("text", ["on: [unclosed", "- just\n- a list\n"])
def test_workflow_events_refuses_a_file_that_is_not_a_yaml_map(text: str) -> None:
    with pytest.raises(ValueError):
        workflow_events(text)


# ---------------------------------------------------------------------------
# ticket_event_workflow_check
# ---------------------------------------------------------------------------


async def test_a_workflow_on_issues_gives_a_warning() -> None:
    result = await _check(
        {
            _DIR: [
                _entry("triage.yml"),
                _entry("ci.yaml"),
                _entry("bot.yml"),
                _entry("README.md"),
                _entry("nested", kind="dir"),
            ],
            f"{_DIR}/triage.yml": _file(_ON_ISSUES),
            f"{_DIR}/ci.yaml": _file(_ON_PUSH),
            f"{_DIR}/bot.yml": _file(_ON_COMMENT),
        }
    )
    assert result["workflow_check"] == "ok"
    assert result["ticket_event_workflows"] == [
        {"path": ".github/workflows/bot.yml", "events": ["issue_comment"]},
        {"path": ".github/workflows/triage.yml", "events": ["issues"]},
    ]
    warning = result["ticket_write_warning"]
    assert ".github/workflows/triage.yml" in warning
    assert ".github/workflows/bot.yml" in warning
    assert "ci.yaml" not in warning


async def test_no_workflow_folder_is_ok_with_no_warning() -> None:
    result = await _check({_DIR: _status_error(404)})
    assert result == {
        "workflow_check": "ok",
        "ticket_event_workflows": [],
        "ticket_write_warning": None,
    }


async def test_workflows_without_ticket_events_give_no_warning() -> None:
    result = await _check({_DIR: [_entry("ci.yml")], f"{_DIR}/ci.yml": _file(_ON_PUSH)})
    assert result["workflow_check"] == "ok"
    assert result["ticket_event_workflows"] == []
    assert result["ticket_write_warning"] is None


@pytest.mark.parametrize(
    "failure", [_status_error(403), _status_error(500), httpx.ConnectError("x")]
)
async def test_a_failed_folder_read_is_unknown_not_empty(failure: Exception) -> None:
    result = await _check({_DIR: failure})
    assert result["workflow_check"] == "unknown"
    assert result["ticket_event_workflows"] is None
    assert "could not check" in result["ticket_write_warning"]
    assert result["workflow_check_error"]


async def test_an_answer_that_is_not_a_folder_listing_is_unknown() -> None:
    result = await _check({_DIR: {"type": "file", "path": ".github/workflows"}})
    assert result["workflow_check"] == "unknown"
    assert result["ticket_event_workflows"] is None


async def test_an_unreadable_file_makes_the_check_incomplete() -> None:
    result = await _check(
        {
            _DIR: [_entry("big.yml"), _entry("broken.yml"), _entry("gone.yml"), _entry("ok.yml")],
            f"{_DIR}/big.yml": {"content": "", "encoding": "none"},
            f"{_DIR}/broken.yml": _file("on: [unclosed"),
            f"{_DIR}/gone.yml": _status_error(404),
            f"{_DIR}/ok.yml": _file(_ON_PUSH),
        }
    )
    assert result["workflow_check"] == "incomplete"
    assert result["ticket_event_workflows"] == []
    assert "3 workflow file(s) could not be read" in result["workflow_check_error"]
    assert "could not be read" in result["ticket_write_warning"]


async def test_more_files_than_the_limit_makes_the_check_incomplete() -> None:
    names = [f"w{n:03}.yml" for n in range(MAX_WORKFLOW_FILES + 2)]
    answers: dict[str, Any] = {_DIR: [_entry(name) for name in names]}
    answers.update({f"{_DIR}/{name}": _file(_ON_PUSH) for name in names})
    session = _Session(answers)
    result = await ticket_event_workflow_check(
        session,  # type: ignore[arg-type]
        object(),
        _operator(),
        _REPO,
    )
    assert len(session.paths) == 1 + MAX_WORKFLOW_FILES
    assert result["workflow_check"] == "incomplete"
    assert "2 workflow file(s)" in result["workflow_check_error"]


# ---------------------------------------------------------------------------
# The probe of a repository target shows the warning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    name: str
    host: str
    port: int | None = None
    secret_ref: str | None = "targets/github/test"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


@respx.mock
async def test_fingerprint_of_a_repository_target_carries_the_warning() -> None:
    async def loader(target: GitHubTargetLike, op: Operator) -> GitHubPATCredentials:
        del target, op
        return GitHubPATCredentials(token="ghp_made_up")

    base = f"{DEFAULT_GITHUB_API_URL}/repos/{_REPO}"
    respx.get(base).mock(
        return_value=httpx.Response(
            200,
            json={"id": 1, "full_name": _REPO, "owner": {"login": "acme", "type": "Organization"}},
        )
    )
    respx.get(f"{base}/contents/.github/workflows").mock(
        return_value=httpx.Response(200, json=[_entry("triage.yml")])
    )
    respx.get(f"{base}/contents/.github/workflows/triage.yml").mock(
        return_value=httpx.Response(200, json=_file(_ON_ISSUES))
    )
    connector = GitHubRestConnector(credentials_loader=loader)
    fingerprint = await connector.fingerprint(
        _Target(name="gh-acme", host=f"api.github.com/repos/{_REPO}"), _operator()
    )
    assert fingerprint.reachable is True
    assert fingerprint.extras["repo_full_name"] == _REPO
    assert fingerprint.extras["ticket_event_workflows"] == [
        {"path": ".github/workflows/triage.yml", "events": ["issues"]}
    ]
    assert "triage.yml" in fingerprint.extras["ticket_write_warning"]
    await connector.aclose()
