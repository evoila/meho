# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The owner and repository name check of the gh-rest composites.

``gh.composite.pr_status_summary`` and ``gh.composite.repo_view`` put
``owner`` and ``repo`` into a REST path. Both are refused before any request
when they are not plain GitHub names: by the schema pattern and the registered
params check (the dispatcher answers ``invalid_params``), and by the handler
itself. The guard at the end makes sure that every composite with an ``owner``
or ``repo`` parameter has the shared check.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._names import (
    check_github_name,
    owner_repo_params_problem,
)
from meho_backplane.connectors.github.composites._read import (
    pr_status_summary_composite,
    repo_view_composite,
)
from meho_backplane.connectors.github.composites._register import (
    _COMPOSITES,
    NAME_CHECKED_OP_IDS,
)
from meho_backplane.connectors.github.composites.schemas import (
    PR_STATUS_SUMMARY_PARAMETER_SCHEMA,
    REPO_VIEW_PARAMETER_SCHEMA,
)
from meho_backplane.operations._validate import (
    _PARAMS_CHECKS,
    params_errors,
    validate_params,
)

_PR_STATUS_SUMMARY = "gh.composite.pr_status_summary"
_REPO_VIEW = "gh.composite.repo_view"

_SCHEMAS: dict[str, dict[str, Any]] = {
    _PR_STATUS_SUMMARY: PR_STATUS_SUMMARY_PARAMETER_SCHEMA,
    _REPO_VIEW: REPO_VIEW_PARAMETER_SCHEMA,
}
_HANDLERS = {
    _PR_STATUS_SUMMARY: pr_status_summary_composite,
    _REPO_VIEW: repo_view_composite,
}

# (owner, repo) pairs that are not plain GitHub names.
_BAD_NAMES = [
    pytest.param("..", "example-repo", id="owner-dot-dot"),
    pytest.param("example-org", "example-repo?per_page=1", id="repo-with-question-mark"),
    pytest.param(".", "example-repo", id="owner-dot"),
    pytest.param("example-org", "..", id="repo-dot-dot"),
    pytest.param("example-org/other", "example-repo", id="owner-with-slash"),
    pytest.param("example-org", "example-repo#x", id="repo-with-hash"),
    pytest.param("example-org", "example-repo\n", id="repo-with-newline"),
]


def _operator() -> Operator:
    return Operator(
        sub="op-gh-composite-names",
        name="GH Composite Names Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


class _GitHubStandIn:
    """Stand-in for api.github.com: records every request and answers with fixed JSON."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.requests.append(path)
        if path.endswith("/check-runs"):
            return {"total_count": 0, "check_runs": []}
        if path.endswith("/reviews"):
            return []
        if "/pulls/" in path:
            return {"number": 7, "head": {"sha": "abc123"}, "mergeable": True}
        return {"name": "example-repo", "owner": {"login": "example-org", "type": "User"}}


def _params(op_id: str, owner: str, repo: str) -> dict[str, Any]:
    params: dict[str, Any] = {"owner": owner, "repo": repo}
    if op_id == _PR_STATUS_SUMMARY:
        params["pull_number"] = 7
    return params


def _dispatcher_refusal(op_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``invalid_params`` entries the dispatcher computes for *params*."""
    return params_errors(op_id, params, validate_params(_SCHEMAS[op_id], params))


async def _call(op_id: str, params: dict[str, Any], github: _GitHubStandIn) -> dict[str, Any]:
    """Run one call the way the dispatcher does: refuse bad params, else run the handler."""
    problems = _dispatcher_refusal(op_id, params)
    if problems:
        return {"status": "invalid_params", "validation_errors": problems}
    return await _HANDLERS[op_id](
        operator=_operator(),
        target=object(),
        params=params,
        connector=github,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_PR_STATUS_SUMMARY, _REPO_VIEW])
@pytest.mark.parametrize(("owner", "repo"), _BAD_NAMES)
async def test_bad_name_is_refused_before_any_request(op_id: str, owner: str, repo: str) -> None:
    github = _GitHubStandIn()
    result = await _call(op_id, _params(op_id, owner, repo), github)
    assert result["status"] == "invalid_params"
    paths = {err["path"] for err in result["validation_errors"]}
    assert paths <= {"owner", "repo", "$.owner", "$.repo"}
    assert github.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_PR_STATUS_SUMMARY, _REPO_VIEW])
@pytest.mark.parametrize(("owner", "repo"), _BAD_NAMES)
async def test_handler_refuses_a_bad_name_on_its_own(op_id: str, owner: str, repo: str) -> None:
    github = _GitHubStandIn()
    with pytest.raises(ValueError, match=r"owner|repo"):
        await _HANDLERS[op_id](
            operator=_operator(),
            target=object(),
            params=_params(op_id, owner, repo),
            connector=github,  # type: ignore[arg-type]
        )
    assert github.requests == []


@pytest.mark.asyncio
async def test_pr_status_summary_with_plain_names_still_reads_github() -> None:
    github = _GitHubStandIn()
    params = _params(_PR_STATUS_SUMMARY, "example-org", ".github")
    result = await _call(_PR_STATUS_SUMMARY, params, github)
    assert result["pr"]["number"] == 7
    assert github.requests[0] == "/repos/example-org/.github/pulls/7"
    assert len(github.requests) == 3


@pytest.mark.asyncio
async def test_repo_view_with_plain_names_still_reads_github() -> None:
    github = _GitHubStandIn()
    result = await _call(_REPO_VIEW, _params(_REPO_VIEW, "Example_Org", "example.repo-1"), github)
    assert result["name"] == "example-repo"
    assert github.requests == ["/repos/Example_Org/example.repo-1"]


def test_the_params_check_names_the_field_and_not_the_value() -> None:
    problem = owner_repo_params_problem({"owner": "example-org", "repo": "bad?value"})
    assert problem is not None
    assert problem["path"] == "repo"
    assert problem["validator"] == "params_check"
    assert "bad?value" not in problem["expected"] + problem["remediation"]
    assert owner_repo_params_problem({"owner": "example-org", "repo": "example-repo"}) is None


def test_composites_with_owner_or_repo_all_use_the_shared_check() -> None:
    """Guard: a composite with an ``owner`` or ``repo`` parameter has the shared check.

    Fails for a new composite that is left out of the registration, and when
    the check of one composite is taken out or replaced.
    """
    with_names = [
        spec
        for spec in _COMPOSITES
        if {"owner", "repo"} & set(spec.parameter_schema.get("properties", {}))
    ]
    assert {spec.op_id for spec in with_names} >= {_PR_STATUS_SUMMARY, _REPO_VIEW}
    for spec in with_names:
        assert spec.op_id in NAME_CHECKED_OP_IDS, spec.op_id
        assert _PARAMS_CHECKS.get(spec.op_id) is owner_repo_params_problem, spec.op_id
        for key in {"owner", "repo"} & set(spec.parameter_schema["properties"]):
            assert params_errors(spec.op_id, {key: ".."}, []), (spec.op_id, key)


@pytest.mark.parametrize("schema", [PR_STATUS_SUMMARY_PARAMETER_SCHEMA, REPO_VIEW_PARAMETER_SCHEMA])
def test_both_read_schemas_carry_the_name_pattern(schema: dict[str, Any]) -> None:
    for key in ("owner", "repo"):
        assert "pattern" in schema["properties"][key], key


@pytest.mark.parametrize("name", ["meho", ".github", "a-b_c.d", "A1"])
def test_shared_check_accepts_plain_names(name: str) -> None:
    assert check_github_name(name) is None
