# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the gh-rest git composites and the commit preview.

The handlers run against a recording session stub with saved responses;
no live GitHub. Coverage:

* ``git_ref_view`` builds ``heads/<name>`` / ``tags/<name>`` itself and
  answers ``found=false`` on 404.
* ``git_tree_view`` passes ``recursive=1`` only when asked and projects
  each entry to path / type / mode / sha / size.
* ``branch_commit_files`` chains read-ref, read-commit, create-tree,
  create-commit, re-read-ref, update-ref (``force=false``); stops without
  touching the branch when the branch is missing, when it moved after the
  read, or when GitHub refuses the update; refuses bad paths and
  over-size files before any call.
* The park-time preview carries the branch, its current commit and each
  file's path / size / SHA-256, never the content.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.github.composites._git import (
    STATUS_COMMITTED,
    STATUS_INVALID_REQUEST,
    STATUS_PRECONDITION_FAILED,
    branch_commit_files_composite,
    describe_files,
    git_ref_view_composite,
    git_tree_view_composite,
)
from meho_backplane.connectors.github.composites._git_preview import (
    BRANCH_COMMIT_FILES_OP_ID,
    branch_commit_files_preview,
)
from meho_backplane.connectors.github.composites.schemas_git import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext

_BASE_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_BASE_TREE_SHA = "c" * 40
_NEW_TREE_SHA = "d" * 40
_NEW_COMMIT_SHA = "e" * 40

_REF_PATH = "/repos/acme/widgets/git/ref/heads/main"
_COMMIT_PATH = f"/repos/acme/widgets/git/commits/{_BASE_SHA}"
_TREES_PATH = "/repos/acme/widgets/git/trees"
_COMMITS_PATH = "/repos/acme/widgets/git/commits"
_REFS_PATH = "/repos/acme/widgets/git/refs/heads/main"


def _make_operator() -> Operator:
    return Operator(
        sub="op-gh-git",
        name="GH Git Composite Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000c0c0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _http_error(status_code: int, body: dict[str, Any] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(status_code, request=request, json=body)
    return httpx.HTTPStatusError(f"http_{status_code}", request=request, response=response)


def _ref_payload(sha: str = _BASE_SHA, object_type: str = "commit") -> dict[str, Any]:
    return {
        "ref": "refs/heads/main",
        "node_id": "N",
        "url": "https://api.github.com/repos/acme/widgets/git/refs/heads/main",
        "object": {
            "type": object_type,
            "sha": sha,
            "url": f"https://api.github.com/repos/acme/widgets/git/commits/{sha}",
        },
    }


class _RecordingConnector:
    """Session stub for the subset of ``GitHubRestConnector`` the handlers use.

    ``get`` maps a path to a payload, an exception, or a list of payloads
    returned in order (so the second read of a ref can differ from the
    first). ``post`` maps a ``(verb, path)`` pair the same way.
    """

    def __init__(
        self,
        get: dict[str, Any] | None = None,
        post: dict[tuple[str, str], Any] | None = None,
    ) -> None:
        self._get = dict(get or {})
        self._post = dict(post or {})
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[dict[str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return path

    @staticmethod
    def _answer(value: Any) -> Any:
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.get_calls.append((path, params))
        if path not in self._get:
            raise AssertionError(f"unexpected GET {path}")
        return self._answer(self._get[path])

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.post_calls.append({"verb": verb, "path": path, "json": json})
        key = (verb, path)
        if key not in self._post:
            raise AssertionError(f"unexpected {verb} {path}")
        return self._answer(self._post[key])


def _happy_connector(
    *,
    second_ref: dict[str, Any] | None = None,
    patch_response: Any = None,
) -> _RecordingConnector:
    first_ref = _ref_payload()
    return _RecordingConnector(
        get={
            _REF_PATH: [first_ref, second_ref if second_ref is not None else _ref_payload()],
            _COMMIT_PATH: {"sha": _BASE_SHA, "tree": {"sha": _BASE_TREE_SHA}},
        },
        post={
            ("POST", _TREES_PATH): {"sha": _NEW_TREE_SHA, "truncated": False, "tree": []},
            ("POST", _COMMITS_PATH): {
                "sha": _NEW_COMMIT_SHA,
                "html_url": f"https://github.com/acme/widgets/commit/{_NEW_COMMIT_SHA}",
                "tree": {"sha": _NEW_TREE_SHA},
            },
            ("PATCH", _REFS_PATH): patch_response
            if patch_response is not None
            else _ref_payload(_NEW_COMMIT_SHA),
        },
    )


def _commit_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "owner": "acme",
        "repo": "widgets",
        "branch": "main",
        "message": "docs: add notes",
        "files": [
            {"path": "docs/notes.md", "content": "# Notes\n"},
            {"path": "README.md", "content": "hello\n"},
        ],
    }
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# git_ref_view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_ref_view_builds_heads_ref_for_a_branch() -> None:
    connector = _RecordingConnector(get={_REF_PATH: _ref_payload()})
    result = await git_ref_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "acme", "repo": "widgets", "name": "main"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.get_calls == [(_REF_PATH, None)]
    assert result == {
        "found": True,
        "kind": "branch",
        "name": "main",
        "ref": "heads/main",
        "sha": _BASE_SHA,
        "object_type": "commit",
        "url": f"https://api.github.com/repos/acme/widgets/git/commits/{_BASE_SHA}",
    }


@pytest.mark.asyncio
async def test_git_ref_view_builds_tags_ref_for_a_tag() -> None:
    path = "/repos/acme/widgets/git/ref/tags/v1.0"
    connector = _RecordingConnector(get={path: _ref_payload(_OTHER_SHA, object_type="tag")})
    result = await git_ref_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "acme", "repo": "widgets", "name": "v1.0", "kind": "tag"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.get_calls[0][0] == path
    assert result["ref"] == "tags/v1.0"
    assert result["kind"] == "tag"
    assert result["object_type"] == "tag"
    assert result["sha"] == _OTHER_SHA


@pytest.mark.asyncio
async def test_git_ref_view_answers_found_false_on_404() -> None:
    connector = _RecordingConnector(get={_REF_PATH: _http_error(404)})
    result = await git_ref_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "acme", "repo": "widgets", "name": "main"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["found"] is False
    assert result["sha"] is None
    assert result["object_type"] is None


@pytest.mark.asyncio
async def test_git_ref_view_raises_on_other_errors() -> None:
    connector = _RecordingConnector(get={_REF_PATH: _http_error(403)})
    with pytest.raises(httpx.HTTPStatusError):
        await git_ref_view_composite(
            operator=_make_operator(),
            target=object(),
            params={"owner": "acme", "repo": "widgets", "name": "main"},
            connector=connector,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# git_tree_view
# ---------------------------------------------------------------------------

_TREE_PAYLOAD: dict[str, Any] = {
    "sha": _BASE_TREE_SHA,
    "url": "https://api.github.com/repos/acme/widgets/git/trees/" + _BASE_TREE_SHA,
    "truncated": False,
    "tree": [
        {"path": "README.md", "mode": "100644", "type": "blob", "sha": "1" * 40, "size": 12},
        {"path": "docs", "mode": "040000", "type": "tree", "sha": "2" * 40},
    ],
}


@pytest.mark.asyncio
async def test_git_tree_view_lists_one_level_without_recursive() -> None:
    path = "/repos/acme/widgets/git/trees/main"
    connector = _RecordingConnector(get={path: _TREE_PAYLOAD})
    result = await git_tree_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "acme", "repo": "widgets", "tree": "main"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.get_calls == [(path, None)]
    assert result == {
        "sha": _BASE_TREE_SHA,
        "recursive": False,
        "truncated": False,
        "entry_count": 2,
        "entries": [
            {"path": "README.md", "type": "blob", "mode": "100644", "sha": "1" * 40, "size": 12},
            {"path": "docs", "type": "tree", "mode": "040000", "sha": "2" * 40, "size": None},
        ],
    }


@pytest.mark.asyncio
async def test_git_tree_view_sends_recursive_query_when_asked() -> None:
    path = f"/repos/acme/widgets/git/trees/{_BASE_TREE_SHA}"
    connector = _RecordingConnector(get={path: {**_TREE_PAYLOAD, "truncated": True}})
    result = await git_tree_view_composite(
        operator=_make_operator(),
        target=object(),
        params={"owner": "acme", "repo": "widgets", "tree": _BASE_TREE_SHA, "recursive": True},
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.get_calls == [(path, {"recursive": "1"})]
    assert result["recursive"] is True
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# branch_commit_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_commit_files_chains_the_git_calls_and_never_forces() -> None:
    connector = _happy_connector()
    result = await branch_commit_files_composite(
        operator=_make_operator(),
        target=object(),
        params=_commit_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == STATUS_COMMITTED
    assert result["reason"] is None
    assert result["base_sha"] == _BASE_SHA
    assert result["tree_sha"] == _NEW_TREE_SHA
    assert result["commit_sha"] == _NEW_COMMIT_SHA
    assert result["html_url"].endswith(_NEW_COMMIT_SHA)
    assert result["file_count"] == 2
    assert result["files"] == [
        {
            "path": "docs/notes.md",
            "size": 8,
            "sha256": hashlib.sha256(b"# Notes\n").hexdigest(),
        },
        {"path": "README.md", "size": 6, "sha256": hashlib.sha256(b"hello\n").hexdigest()},
    ]
    # Two ref reads (before the tree, before the update) and one commit read.
    assert [path for path, _ in connector.get_calls] == [_REF_PATH, _COMMIT_PATH, _REF_PATH]
    tree_call, commit_call, ref_call = connector.post_calls
    assert tree_call["verb"] == "POST"
    assert tree_call["json"] == {
        "base_tree": _BASE_TREE_SHA,
        "tree": [
            {"path": "docs/notes.md", "mode": "100644", "type": "blob", "content": "# Notes\n"},
            {"path": "README.md", "mode": "100644", "type": "blob", "content": "hello\n"},
        ],
    }
    assert commit_call["json"] == {
        "message": "docs: add notes",
        "tree": _NEW_TREE_SHA,
        "parents": [_BASE_SHA],
    }
    assert ref_call["verb"] == "PATCH"
    assert ref_call["path"] == _REFS_PATH
    assert ref_call["json"] == {"sha": _NEW_COMMIT_SHA, "force": False}


@pytest.mark.asyncio
async def test_branch_commit_files_stops_when_the_branch_moved_after_the_read() -> None:
    connector = _happy_connector(second_ref=_ref_payload(_OTHER_SHA))
    result = await branch_commit_files_composite(
        operator=_make_operator(),
        target=object(),
        params=_commit_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == STATUS_PRECONDITION_FAILED
    assert "moved" in result["reason"]
    assert _BASE_SHA in result["reason"] and _OTHER_SHA in result["reason"]
    assert result["commit_sha"] is None
    assert result["base_sha"] == _BASE_SHA
    assert all(call["verb"] != "PATCH" for call in connector.post_calls), (
        "the branch must not be updated when it moved"
    )


@pytest.mark.asyncio
async def test_branch_commit_files_stops_when_github_refuses_the_update() -> None:
    refusal = _http_error(422, {"message": "Update is not a fast forward"})
    connector = _happy_connector(patch_response=refusal)
    result = await branch_commit_files_composite(
        operator=_make_operator(),
        target=object(),
        params=_commit_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == STATUS_PRECONDITION_FAILED
    assert "Update is not a fast forward" in result["reason"]
    assert result["commit_sha"] is None


@pytest.mark.asyncio
async def test_branch_commit_files_raises_on_an_unexpected_update_error() -> None:
    connector = _happy_connector(patch_response=_http_error(500))
    with pytest.raises(httpx.HTTPStatusError):
        await branch_commit_files_composite(
            operator=_make_operator(),
            target=object(),
            params=_commit_params(),
            connector=connector,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_branch_commit_files_stops_when_the_branch_does_not_exist() -> None:
    connector = _RecordingConnector(get={_REF_PATH: _http_error(404)})
    result = await branch_commit_files_composite(
        operator=_make_operator(),
        target=object(),
        params=_commit_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == STATUS_PRECONDITION_FAILED
    assert "does not exist" in result["reason"]
    assert connector.post_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "docs/../secret", "./a", "a//b", "a\\b", ""],
)
async def test_branch_commit_files_refuses_bad_paths_before_any_call(path: str) -> None:
    connector = _RecordingConnector()
    result = await branch_commit_files_composite(
        operator=_make_operator(),
        target=object(),
        params=_commit_params(files=[{"path": path, "content": "x"}]),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == STATUS_INVALID_REQUEST
    assert connector.get_calls == [] and connector.post_calls == []


@pytest.mark.asyncio
async def test_branch_commit_files_refuses_over_size_files_before_any_call() -> None:
    connector = _RecordingConnector()
    big = "x" * (MAX_FILE_BYTES + 1)
    result = await branch_commit_files_composite(
        operator=_make_operator(),
        target=object(),
        params=_commit_params(files=[{"path": "big.txt", "content": big}]),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == STATUS_INVALID_REQUEST
    assert "per file" in result["reason"]
    assert connector.get_calls == [] and connector.post_calls == []


def test_describe_files_counts_utf8_bytes_and_the_total() -> None:
    files = [{"path": "a.txt", "content": "é"}, {"path": "b.txt", "content": "x" * 10}]
    described, problems = describe_files(files)
    assert problems == []
    assert described[0]["size"] == 2
    assert described[1]["size"] == 10


def test_describe_files_flags_duplicates_and_the_total_limit() -> None:
    half = "x" * (MAX_TOTAL_BYTES // 2 + 1)
    files = [
        {"path": "a.txt", "content": half},
        {"path": "a.txt", "content": half},
    ]
    _, problems = describe_files(files)
    assert any("twice" in problem for problem in problems)
    assert any("together" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Park-time preview
# ---------------------------------------------------------------------------


def _preview_ctx(connector: Any, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=MagicMock(op_id=BRANCH_COMMIT_FILES_OP_ID),
        connector_instance=connector,
        operator=_make_operator(),
        target=object(),
        params=params,
        connector_id="gh-rest-3",
    )


def test_preview_builder_is_registered_for_the_write() -> None:
    assert _PREVIEW_BUILDERS.get(BRANCH_COMMIT_FILES_OP_ID) is branch_commit_files_preview


@pytest.mark.asyncio
async def test_preview_shows_branch_base_commit_and_file_digests_only() -> None:
    connector = _RecordingConnector(get={_REF_PATH: _ref_payload()})
    preview = await branch_commit_files_preview(_preview_ctx(connector, _commit_params()))
    assert preview is not None
    assert preview["branch"] == "main"
    assert preview["base_sha"] == _BASE_SHA
    assert preview["file_count"] == 2
    assert preview["total_bytes"] == 14
    assert preview["files"][0] == {
        "path": "docs/notes.md",
        "size": 8,
        "sha256": hashlib.sha256(b"# Notes\n").hexdigest(),
    }
    assert "content" not in str(preview["files"])
    assert "# Notes" not in str(preview)
    assert connector.post_calls == [], "the preview must only read"


@pytest.mark.asyncio
async def test_preview_without_connector_skips_the_live_read() -> None:
    preview = await branch_commit_files_preview(_preview_ctx(None, _commit_params()))
    assert preview is not None
    assert preview["base_sha"] is None
    assert preview["file_count"] == 2


@pytest.mark.asyncio
async def test_preview_names_a_missing_branch_instead_of_failing() -> None:
    connector = _RecordingConnector(get={_REF_PATH: _http_error(404)})
    preview = await branch_commit_files_preview(_preview_ctx(connector, _commit_params()))
    assert preview is not None
    assert preview["base_sha"] is None
    assert "does not exist" in preview["base_sha_error"]


@pytest.mark.asyncio
async def test_preview_declines_without_the_identity_fields() -> None:
    assert await branch_commit_files_preview(_preview_ctx(None, {"files": []})) is None
