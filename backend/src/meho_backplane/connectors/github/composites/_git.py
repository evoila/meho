# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Git composites of the gh-rest connector: read a ref, read a tree, commit files.

Three hand-written operations replace the raw git calls people used to chain
by hand (and often got wrong, because a raw ref call needs the exact
``heads/<branch>`` form):

* ``gh.composite.git_ref_view`` -- the commit a branch or tag points to
  (``GET /repos/{owner}/{repo}/git/ref/{ref}``). MEHO builds the ref.
* ``gh.composite.git_tree_view`` -- the files and folders of a tree
  (``GET /repos/{owner}/{repo}/git/trees/{tree_sha}``, ``recursive=1`` on
  request).
* ``gh.composite.branch_commit_files`` -- write text files to a branch as one
  new commit on top of ``base_sha``, the commit the caller read with
  ``git_ref_view`` and the approver saw: check the branch is still at
  ``base_sha``, read that commit for the base tree, create a tree on top of
  it, create the commit, check the branch again, move it with
  ``force=false``. If the branch is not at ``base_sha`` at any of these
  points, the call stops with ``status="precondition_failed"`` and changes
  nothing visible (a tree or commit object that was already created is
  unreachable and harmless).

Names in the URL path
---------------------

Owner, repository, branch, tag and tree values become URL path parts. Each
is checked first and then percent-encoded with :func:`urllib.parse.quote`,
so a branch like ``fix#1`` reaches GitHub as ``heads/fix%231`` instead of
being cut at the ``#``, and a value can never point at another API path.
Owner and repository names follow GitHub's name rule
(:func:`check_github_name`: letters, digits, ``.``, ``-`` and ``_``, but
not ``.`` or ``..`` alone, so the repo ``.github`` is fine). Branch, tag and
tree values follow git's ``check-ref-format`` rules (:func:`check_name`).

All three use the direct-session substrate: the handler declares a
``connector`` parameter and calls the resolved connector's own session, so
they work on a fresh deploy with no gh catalog ingest.

The write is registered ``safety_level="dangerous"`` /
``requires_approval=True``: every call waits for approval. The approver sees
the park-time preview built in :mod:`._git_preview`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors.github.composites.schemas_git import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = [
    "STATUS_COMMITTED",
    "STATUS_INVALID_REQUEST",
    "STATUS_PRECONDITION_FAILED",
    "GitRepo",
    "branch_commit_files_composite",
    "check_github_name",
    "check_name",
    "describe_files",
    "git_ref_view_composite",
    "git_tree_view_composite",
]

STATUS_COMMITTED: Final = "committed"
STATUS_PRECONDITION_FAILED: Final = "precondition_failed"
STATUS_INVALID_REQUEST: Final = "invalid_request"

# GitHub REST wire paths, keyed with ``str.format`` placeholders. The
# connector mounts them under ``https://api.github.com``; ``mount_op_path``
# is identity for github.com. Every placeholder value is checked and
# percent-encoded before it is formatted in (see ``GitRepo``).
_REF_PATH: Final = "/repos/{owner}/{repo}/git/ref/{ref}"
_REFS_PATH: Final = "/repos/{owner}/{repo}/git/refs/{ref}"
_TREE_PATH: Final = "/repos/{owner}/{repo}/git/trees/{tree}"
_TREES_PATH: Final = "/repos/{owner}/{repo}/git/trees"
_COMMIT_PATH: Final = "/repos/{owner}/{repo}/git/commits/{sha}"
_COMMITS_PATH: Final = "/repos/{owner}/{repo}/git/commits"

_REF_PREFIX_BY_KIND: Final[dict[str, str]] = {"branch": "heads", "tag": "tags"}
_BLOB_MODE: Final = "100644"
_SHA1: Final = re.compile(r"^[0-9a-f]{40}$")

# Characters git refuses in a ref name (check-ref-format), plus control
# characters and space.
_FORBIDDEN_CHARS: Final = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")

# What GitHub allows in a user, organisation or repository name.
_GITHUB_NAME: Final = re.compile(r"[A-Za-z0-9._-]+")

# Status codes GitHub uses to refuse a branch update that is not a fast
# forward (``force=false``). The exact wording is GitHub's; MEHO quotes it.
_REFUSED_UPDATE_STATUSES: Final = frozenset({409, 422})


def check_github_name(value: object) -> str | None:
    """Return why ``value`` is not a safe owner or repository name, or ``None``.

    GitHub names hold letters, digits, ``.``, ``-`` and ``_`` only. The
    names ``.`` and ``..`` are refused because the URL encoding keeps dots,
    so they would point at another API path. ``.github`` is a valid name.
    """
    if not isinstance(value, str) or not value:
        return "name is empty"
    if value in (".", ".."):
        return f"name {value!r} is not allowed"
    if not _GITHUB_NAME.fullmatch(value):
        return f"name {value!r} may hold only letters, digits, '.', '-' and '_'"
    return None


def check_name(value: object, *, allow_slash: bool) -> str | None:
    """Return why ``value`` is not a safe ref or tree name, or ``None``.

    Follows git's ``check-ref-format`` rules: no control characters, space
    or ``~ ^ : ? * [ \\``, no ``..``, no ``@{``, no part that starts with
    ``.`` or ends with ``.lock``, no leading, trailing or double ``/``, no
    trailing ``.``. With ``allow_slash=False`` the value may not contain
    ``/`` at all. A value that passes can only ever name one path part (or,
    with slashes, parts under the same prefix), never a different API path.
    """
    if not isinstance(value, str) or not value:
        return "name is empty"
    if _FORBIDDEN_CHARS.search(value):
        return f"name {value!r} contains a control character, space or one of ~ ^ : ? * [ \\"
    if ".." in value or "@{" in value:
        return f"name {value!r} contains '..' or '@{{'"
    if value.endswith(".") or value.endswith("/") or value.startswith("/"):
        return f"name {value!r} starts or ends with '/' or ends with '.'"
    if not allow_slash and "/" in value:
        return f"name {value!r} contains '/'"
    for part in value.split("/"):
        if not part:
            return f"name {value!r} contains '//'"
        if part.startswith(".") or part.endswith(".lock"):
            return f"name {value!r} has a part that starts with '.' or ends with '.lock'"
    return None


def _checked(value: object, *, what: str, allow_slash: bool) -> str:
    problem = check_name(value, allow_slash=allow_slash)
    if problem is not None:
        raise ValueError(f"{what}: {problem}")
    return str(value)


def _checked_github_name(value: object, *, what: str) -> str:
    problem = check_github_name(value)
    if problem is not None:
        raise ValueError(f"{what}: {problem}")
    return str(value)


@dataclass(frozen=True)
class GitRepo:
    """One repository reached through the resolved connector's session.

    ``owner`` and ``repo`` are checked on creation. Path parts passed to
    :meth:`get` / :meth:`send` are percent-encoded (``/`` kept for refs).
    """

    connector: GitHubRestConnector
    target: Any
    operator: Operator
    owner: str
    repo: str

    def __post_init__(self) -> None:
        _checked_github_name(self.owner, what="owner")
        _checked_github_name(self.repo, what="repo")

    async def _path(self, template: str, **fields: str) -> str:
        encoded = {name: quote(value, safe="/") for name, value in fields.items()}
        path = template.format(
            owner=quote(self.owner, safe=""), repo=quote(self.repo, safe=""), **encoded
        )
        return await self.connector.mount_op_path(self.target, path, self.operator)

    async def get(
        self, template: str, *, params: dict[str, Any] | None = None, **fields: str
    ) -> dict[str, Any]:
        path = await self._path(template, **fields)
        return await self.connector._get_json(
            self.target, path, operator=self.operator, params=params
        )

    async def send(
        self, verb: str, template: str, *, json: dict[str, Any], **fields: str
    ) -> dict[str, Any]:
        path = await self._path(template, **fields)
        return await self.connector._post_json(
            self.target, path, operator=self.operator, verb=verb, json=json
        )

    async def read_ref(self, ref: str) -> dict[str, Any] | None:
        """Return GitHub's payload for ``ref`` (``heads/x`` / ``tags/x``), or ``None`` on 404."""
        try:
            return await self.get(_REF_PATH, ref=ref)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def ref_sha(self, ref: str) -> str | None:
        """Return the SHA-1 the ref points to, or ``None`` when the ref does not exist."""
        payload = await self.read_ref(ref)
        return _object_sha(payload)


def _object_sha(payload: dict[str, Any] | None) -> str | None:
    obj = payload.get("object") if isinstance(payload, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    return sha if isinstance(sha, str) and sha else None


def _github_message(exc: httpx.HTTPStatusError) -> str:
    """Return GitHub's ``message`` from an error response, or the status text."""
    try:
        payload = exc.response.json()
    except ValueError:
        payload = None
    message = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(message, str) and message:
        return message
    return f"HTTP {exc.response.status_code}"


def _repo(
    connector: GitHubRestConnector, target: Any, operator: Operator, params: dict[str, Any]
) -> GitRepo:
    return GitRepo(connector, target, operator, params["owner"], params["repo"])


async def git_ref_view_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Return the commit a branch or tag points to (``gh.composite.git_ref_view``).

    Raises :class:`ValueError` when the owner, repository or name is not a
    safe path part (see :func:`check_name`).
    """
    kind = params.get("kind", "branch")
    name = _checked(params["name"], what=kind, allow_slash=True)
    ref = f"{_REF_PREFIX_BY_KIND[kind]}/{name}"
    payload = await _repo(connector, target, operator, params).read_ref(ref)
    obj = payload.get("object") if isinstance(payload, dict) else None
    obj = obj if isinstance(obj, dict) else {}
    return {
        "found": payload is not None,
        "kind": kind,
        "name": name,
        "ref": ref,
        "sha": obj.get("sha"),
        "object_type": obj.get("type"),
        "url": obj.get("url"),
    }


async def git_tree_view_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Return the files and folders of a tree (``gh.composite.git_tree_view``).

    Raises :class:`ValueError` when the owner, repository or tree value is
    not a safe path part (see :func:`check_name`).
    """
    recursive = bool(params.get("recursive", False))
    tree = _checked(params["tree"], what="tree", allow_slash=True)
    payload = await _repo(connector, target, operator, params).get(
        _TREE_PATH, params={"recursive": "1"} if recursive else None, tree=tree
    )
    raw_entries = payload.get("tree")
    entries = [
        {
            "path": entry.get("path"),
            "type": entry.get("type"),
            "mode": entry.get("mode"),
            "sha": entry.get("sha"),
            "size": entry.get("size"),
        }
        for entry in (raw_entries if isinstance(raw_entries, list) else [])
        if isinstance(entry, dict)
    ]
    return {
        "sha": payload.get("sha"),
        "recursive": recursive,
        "truncated": bool(payload.get("truncated", False)),
        "entry_count": len(entries),
        "entries": entries,
    }


def _path_problem(path: object) -> str | None:
    if not isinstance(path, str) or not path:
        return "path is empty"
    if path.startswith("/"):
        return f"path {path!r} starts with '/'"
    if "\\" in path:
        return f"path {path!r} contains a backslash"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return f"path {path!r} has an empty, '.' or '..' part"
    return None


def describe_files(files: object) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``[{path, size, sha256}]`` for the files, and the problems found.

    Shared by the handler and the park-time preview, so the approver reads
    exactly the file list the commit would write. Never includes content.
    """
    if not isinstance(files, list) or not files:
        return [], ["files must be a non-empty list"]
    described: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()
    total = 0
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files; at most {MAX_FILES} per call")
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        content = entry.get("content") if isinstance(entry, dict) else None
        problem = _path_problem(path)
        if problem is not None:
            problems.append(problem)
        elif path in seen:
            problems.append(f"path {path!r} appears twice")
        else:
            seen.add(str(path))
        if not isinstance(content, str):
            problems.append(f"content of {path!r} is not text")
            continue
        data = content.encode("utf-8")
        total += len(data)
        if len(data) > MAX_FILE_BYTES:
            problems.append(f"{path!r} has {len(data)} bytes; at most {MAX_FILE_BYTES} per file")
        described.append(
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    if total > MAX_TOTAL_BYTES:
        problems.append(f"all files together have {total} bytes; at most {MAX_TOTAL_BYTES}")
    return described, problems


def check_commit_request(params: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Check the names, ``base_sha`` and files of a commit call before any vendor call.

    Shared by the handler and the park-time preview. Returns the described
    files and the problems found (empty when the request is well-formed).
    """
    problems: list[str] = []
    for key in ("owner", "repo"):
        problem = check_github_name(params.get(key))
        if problem is not None:
            problems.append(f"{key}: {problem}")
    branch_problem = check_name(params.get("branch"), allow_slash=True)
    if branch_problem is not None:
        problems.append(f"branch: {branch_problem}")
    base_sha = params.get("base_sha")
    if not isinstance(base_sha, str) or not _SHA1.match(base_sha):
        problems.append("base_sha must be the 40-character lowercase SHA-1 of a commit")
    files, file_problems = describe_files(params.get("files"))
    return files, problems + file_problems


def _stopped(
    status: str, reason: str, *, branch: str, base_sha: str | None, files: list[dict[str, Any]]
) -> dict[str, Any]:
    """The envelope of a call that stopped before the branch was moved."""
    return {
        "status": status,
        "reason": reason,
        "branch": branch,
        "base_sha": base_sha,
        "tree_sha": None,
        "commit_sha": None,
        "html_url": None,
        "file_count": len(files),
        "files": files,
    }


async def _create_commit(
    repo: GitRepo, *, base_sha: str, message: str, files: list[dict[str, Any]]
) -> dict[str, Any]:
    """Create a tree on top of ``base_sha``'s tree and a commit with ``base_sha`` as parent."""
    base_commit = await repo.get(_COMMIT_PATH, sha=base_sha)
    base_tree_sha = base_commit.get("tree", {}).get("sha")
    if not isinstance(base_tree_sha, str) or not base_tree_sha:
        raise RuntimeError(
            f"GitHub returned no tree for commit {base_sha} of {repo.owner}/{repo.repo}"
        )
    tree = await repo.send(
        "POST",
        _TREES_PATH,
        json={
            "base_tree": base_tree_sha,
            "tree": [
                {
                    "path": entry["path"],
                    "mode": _BLOB_MODE,
                    "type": "blob",
                    "content": entry["content"],
                }
                for entry in files
            ],
        },
    )
    return await repo.send(
        "POST",
        _COMMITS_PATH,
        json={"message": message, "tree": tree["sha"], "parents": [base_sha]},
    )


def _moved(base_sha: str, sha_now: str | None) -> str:
    if sha_now is None:
        return f"branch is at {base_sha} no more: it does not exist; nothing changed"
    return f"branch is at {sha_now}, not at base_sha {base_sha}; nothing changed"


async def _move_branch(repo: GitRepo, *, ref: str, base_sha: str, commit_sha: str) -> str | None:
    """Move ``ref`` from ``base_sha`` to ``commit_sha`` without force.

    Returns ``None`` on success, else the reason the branch was not moved.
    """
    sha_now = await repo.ref_sha(ref)
    if sha_now != base_sha:
        return _moved(base_sha, sha_now)
    try:
        await repo.send("PATCH", _REFS_PATH, json={"sha": commit_sha, "force": False}, ref=ref)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in _REFUSED_UPDATE_STATUSES:
            raise
        return (
            f"GitHub refused to move the branch without force: {_github_message(exc)}; "
            "nothing changed"
        )
    return None


async def branch_commit_files_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Write text files to a branch as one commit (``gh.composite.branch_commit_files``).

    Steps: check names, ``base_sha`` and files (``invalid_request`` on a
    problem, nothing called); read the branch and stop with
    ``precondition_failed`` unless it is at ``base_sha``; read that commit
    for the base tree; create a tree on top of it with the files; create
    the commit with ``base_sha`` as parent; read the branch again and move
    it with ``force=false``. Any step that finds the branch gone or moved
    returns ``status="precondition_failed"`` and the branch is not touched.
    A transport error or an unexpected status raises, so the dispatcher
    records the call as failed.
    """
    branch = str(params.get("branch", ""))
    files, problems = check_commit_request(params)
    if problems:
        return _stopped(
            STATUS_INVALID_REQUEST, "; ".join(problems), branch=branch, base_sha=None, files=files
        )
    base_sha = params["base_sha"]
    ref = f"heads/{branch}"
    repo = _repo(connector, target, operator, params)
    sha_now = await repo.ref_sha(ref)
    if sha_now != base_sha:
        return _stopped(
            STATUS_PRECONDITION_FAILED,
            f"branch {branch!r}: {_moved(base_sha, sha_now)}",
            branch=branch,
            base_sha=base_sha,
            files=files,
        )
    commit = await _create_commit(
        repo, base_sha=base_sha, message=params["message"], files=params["files"]
    )
    reason = await _move_branch(repo, ref=ref, base_sha=base_sha, commit_sha=commit["sha"])
    if reason is not None:
        return _stopped(
            STATUS_PRECONDITION_FAILED,
            f"branch {branch!r}: {reason}",
            branch=branch,
            base_sha=base_sha,
            files=files,
        )
    return {
        "status": STATUS_COMMITTED,
        "reason": None,
        "branch": branch,
        "base_sha": base_sha,
        "tree_sha": commit.get("tree", {}).get("sha"),
        "commit_sha": commit["sha"],
        "html_url": commit.get("html_url"),
        "file_count": len(files),
        "files": files,
    }
