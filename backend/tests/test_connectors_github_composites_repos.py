# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``gh.composite.repo_create`` (direct-session REST write).

The composite reads ``GET /repos/{org}/{name}`` first and only writes
``POST /orgs/{org}/repos`` when that read answers 404. These tests drive
the handler with a recording session stub and saved answers -- no live
GitHub.

Coverage:

* Happy path -- a 404 on the read leads to one POST with the documented
  body; the result keeps only the summary fields and ``status="created"``.
* Visibility -- private when omitted; public only when asked for by name.
* Existing name -- the read succeeds, nothing is posted, and the result is
  ``status="unchanged"`` with the existing repository's facts.
* A read error other than 404, a POST error, and a non-object payload all
  propagate for the dispatcher's outer branch to classify.
* Names in the request path -- ``org`` and ``name`` are checked before
  the first request. A value with a path separator, ``.`` or ``..``, or
  any other character GitHub does not allow, raises and sends nothing.
  The parameter schema refuses the same values, so the dispatcher stops
  them as ``invalid_params`` before the handler runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._repos import (
    check_github_name,
    repo_create_composite,
)
from meho_backplane.connectors.github.composites.schemas import (
    REPO_CREATE_PARAMETER_SCHEMA,
)
from meho_backplane.operations._validate import validate_params

_CREATE_RESPONSE = Path(__file__).parent / "data" / "github_repo_create_response.json"
_VIEW_RESPONSE = Path(__file__).parent / "data" / "github_repo_view_response.json"


def _saved(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _make_operator() -> Operator:
    """Synthetic operator for composite-handler unit tests."""
    return Operator(
        sub="op-gh-repo-create",
        name="GH Repo Create Composite Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000d0d0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _http_error(status_code: int, method: str = "GET") -> httpx.HTTPStatusError:
    """Build an ``httpx.HTTPStatusError`` the way ``raise_for_status`` would."""
    request = httpx.Request(method, "https://api.github.com/x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"http_{status_code}", request=request, response=response)


class _RecordingConnector:
    """Session stub matching the subset of ``GitHubRestConnector`` the handler uses.

    ``get_response`` answers the existence read (a payload, or an exception
    to raise); ``post_response`` answers the create call the same way.
    """

    def __init__(self, get_response: Any, post_response: Any = None) -> None:
        self._get_response = get_response
        self._post_response = post_response
        self.get_paths: list[str] = []
        self.post_calls: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    async def _get_json(self, target: Any, path: str, *, operator: Operator) -> Any:
        self.get_paths.append(path)
        if isinstance(self._get_response, Exception):
            raise self._get_response
        return self._get_response

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.post_calls.append({"path": path, "verb": verb, "json": json})
        if isinstance(self._post_response, Exception):
            raise self._post_response
        return self._post_response


async def _run(connector: _RecordingConnector, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"org": "example-org", "name": "new-repo", **extra}
    return await repo_create_composite(
        operator=_make_operator(),
        target=object(),
        params=params,
        connector=connector,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_repo_create_posts_once_and_returns_the_summary() -> None:
    """A 404 on the read leads to one POST; the result keeps the summary fields."""
    connector = _RecordingConnector(_http_error(404), _saved(_CREATE_RESPONSE))

    result = await _run(connector, description="A new example repository.", with_readme=True)

    assert connector.get_paths == ["/repos/example-org/new-repo"]
    assert connector.post_calls == [
        {
            "path": "/orgs/example-org/repos",
            "verb": "POST",
            "json": {
                "name": "new-repo",
                "visibility": "private",
                "private": True,
                "auto_init": True,
                "description": "A new example repository.",
            },
        }
    ]
    assert result == {
        "status": "created",
        "name": "new-repo",
        "full_name": "example-org/new-repo",
        "owner": "example-org",
        "url": "https://github.com/example-org/new-repo",
        "visibility": "private",
        "default_branch": "main",
        "guidance": "repository example-org/new-repo was created with visibility 'private'",
    }


@pytest.mark.asyncio
async def test_repo_create_is_private_and_empty_when_only_the_name_is_given() -> None:
    connector = _RecordingConnector(_http_error(404), _saved(_CREATE_RESPONSE))

    await _run(connector)

    body = connector.post_calls[0]["json"]
    assert body == {
        "name": "new-repo",
        "visibility": "private",
        "private": True,
        "auto_init": False,
    }


@pytest.mark.asyncio
async def test_repo_create_is_public_only_when_asked_by_name() -> None:
    payload = _saved(_CREATE_RESPONSE)
    payload["private"] = False
    payload["visibility"] = "public"
    connector = _RecordingConnector(_http_error(404), payload)

    result = await _run(connector, visibility="public")

    body = connector.post_calls[0]["json"]
    assert body["visibility"] == "public"
    assert body["private"] is False
    assert result["visibility"] == "public"


@pytest.mark.asyncio
async def test_repo_create_changes_nothing_when_the_name_exists() -> None:
    """An existing repository answers the read: no POST, status 'unchanged'."""
    existing = _saved(_VIEW_RESPONSE)
    existing["name"] = "new-repo"
    existing["full_name"] = "example-org/new-repo"
    existing["html_url"] = "https://github.com/example-org/new-repo"
    connector = _RecordingConnector(existing, _saved(_CREATE_RESPONSE))

    result = await _run(connector)

    assert connector.post_calls == []
    assert result == {
        "status": "unchanged",
        "name": "new-repo",
        "full_name": "example-org/new-repo",
        "owner": "example-org",
        "url": "https://github.com/example-org/new-repo",
        "visibility": "public",
        "default_branch": "main",
        "guidance": (
            "a repository named 'new-repo' already exists in organization "
            "'example-org'; nothing was written"
        ),
    }


@pytest.mark.asyncio
async def test_repo_create_propagates_a_read_error_other_than_404() -> None:
    connector = _RecordingConnector(_http_error(403), _saved(_CREATE_RESPONSE))

    with pytest.raises(httpx.HTTPStatusError):
        await _run(connector)

    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_repo_create_propagates_a_post_error() -> None:
    connector = _RecordingConnector(_http_error(404), _http_error(422, "POST"))

    with pytest.raises(httpx.HTTPStatusError):
        await _run(connector)

    assert len(connector.post_calls) == 1


@pytest.mark.asyncio
async def test_repo_create_rejects_a_payload_that_is_not_an_object() -> None:
    with pytest.raises(RuntimeError, match="repo_create"):
        await _run(_RecordingConnector(["not", "an", "object"]))
    with pytest.raises(RuntimeError, match="repo_create"):
        await _run(_RecordingConnector(_http_error(404), ["not", "an", "object"]))


# ---------------------------------------------------------------------------
# Names in the request path
# ---------------------------------------------------------------------------

_UNSAFE_NAMES = (
    "../user",
    "..",
    ".",
    "example-org/../../user",
    "example-org/repos",
    "a b",
    "a#b",
    "a?x=1",
    "a%2F..",
    "a\n",
    "",
)


@pytest.mark.parametrize("org", _UNSAFE_NAMES)
@pytest.mark.asyncio
async def test_repo_create_sends_nothing_when_the_org_is_not_a_safe_name(org: str) -> None:
    """A bad ``org`` raises before the first request; no path is built from it."""
    connector = _RecordingConnector(_http_error(404), _saved(_CREATE_RESPONSE))

    with pytest.raises(ValueError, match="repo_create: org"):
        await _run(connector, org=org)

    assert connector.get_paths == []
    assert connector.post_calls == []


@pytest.mark.parametrize("name", _UNSAFE_NAMES)
@pytest.mark.asyncio
async def test_repo_create_sends_nothing_when_the_name_is_not_a_safe_name(name: str) -> None:
    """A bad ``name`` raises before the first request; no path is built from it."""
    connector = _RecordingConnector(_http_error(404), _saved(_CREATE_RESPONSE))

    with pytest.raises(ValueError, match="repo_create: name"):
        await _run(connector, name=name)

    assert connector.get_paths == []
    assert connector.post_calls == []


@pytest.mark.asyncio
async def test_repo_create_request_paths_hold_exactly_the_checked_names() -> None:
    """``.github`` is a valid name; both request paths carry it as one path part."""
    connector = _RecordingConnector(_http_error(404), _saved(_CREATE_RESPONSE))

    await _run(connector, org="My.Org_1", name=".github")

    assert connector.get_paths == ["/repos/My.Org_1/.github"]
    assert [call["path"] for call in connector.post_calls] == ["/orgs/My.Org_1/repos"]
    assert connector.post_calls[0]["json"]["name"] == ".github"


@pytest.mark.parametrize("value", _UNSAFE_NAMES)
def test_check_github_name_refuses_a_value_that_could_change_the_path(value: str) -> None:
    assert check_github_name(value) is not None


@pytest.mark.parametrize("value", ["a", ".github", "my-repo_1.0", "Org"])
def test_check_github_name_accepts_a_plain_github_name(value: str) -> None:
    assert check_github_name(value) is None


@pytest.mark.parametrize("field", ["org", "name"])
@pytest.mark.parametrize("value", [v for v in _UNSAFE_NAMES if v not in (".", "..")])
def test_repo_create_schema_refuses_a_name_that_could_change_the_path(
    field: str, value: str
) -> None:
    """The dispatcher stops these as ``invalid_params`` before the handler runs."""
    params = {"org": "example-org", "name": "new-repo", field: value}

    errors = validate_params(REPO_CREATE_PARAMETER_SCHEMA, params)

    assert errors, f"{field}={value!r} passed the schema"
    assert all(error["path"].endswith(field) for error in errors), errors


def test_repo_create_schema_accepts_a_plain_github_name() -> None:
    params = {"org": "My.Org_1", "name": ".github", "visibility": "private"}

    assert validate_params(REPO_CREATE_PARAMETER_SCHEMA, params) == []
