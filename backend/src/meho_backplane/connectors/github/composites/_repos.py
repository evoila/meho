# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Repository write ``gh.composite.*`` handler: ``repo_create``.

People create a repository in an organization through the raw operation
``POST:/orgs/{org}/repos``. That raw operation has no summary and many
raw operations look alike, so calls fail. This module adds the typed
write ``gh.composite.repo_create``, with a clear summary, fields and
risk level.

The handler uses the same direct-session substrate as the other
composites (``connector._get_json`` / ``connector._post_json`` through
``mount_op_path``), so it works on a fresh deploy with no gh catalog
ingest.

Safe to run twice
-----------------

Before it writes, the handler reads ``GET /repos/{org}/{name}``. When
that read succeeds, a repository with that name already exists: the
handler returns ``status="unchanged"`` with the existing repository's
facts and a clear message, and writes nothing. Only a 404 on that read
leads to the create call. Any other error on the read propagates.

Names in the request path
-------------------------

``org`` and ``name`` become parts of the request path. The parameter
schema already limits both to letters, digits, ``.``, ``-`` and ``_``.
The handler checks them again before the first request and refuses
``.`` and ``..``, which the character rule allows but which would point
at another API path. A bad name raises :class:`ValueError` and no
request is sent. Both values are then percent-encoded, so a value can
only ever name one path part.

Governance posture
------------------

A change, registered ``safety_level="caution"`` /
``requires_approval=True`` (see :mod:`._register`). Every write waits
for approval. The composite is the reviewed unit: the dispatcher gates
it via ``policy_gate`` before invoking the handler, so the one write it
performs is fully governed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = ["check_github_name", "repo_create_composite"]

# REST wire paths, keyed with ``str.format`` placeholders. Mounted under
# ``https://api.github.com`` by the connector's ``_base_url``;
# ``mount_op_path`` is identity for github.com. Every placeholder value
# is checked and percent-encoded before it is formatted in.
_ORG_REPOS_PATH: Final = "/orgs/{org}/repos"
_REPO_PATH: Final = "/repos/{org}/{name}"

# What GitHub allows in a user, organisation or repository name.
_GITHUB_NAME: Final = re.compile(r"[A-Za-z0-9._-]+")


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


def _checked_path_part(field: str, value: object) -> str:
    """Return ``value`` as a safe, percent-encoded path part, or raise ``ValueError``."""
    problem = check_github_name(value)
    if problem is not None:
        raise ValueError(f"repo_create: {field}: {problem}")
    return quote(str(value), safe="")


def _project_created_repo(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields the caller needs from a repository payload."""
    raw_owner = payload.get("owner")
    owner: dict[str, Any] = raw_owner if isinstance(raw_owner, dict) else {}
    return {
        "name": payload.get("name"),
        "full_name": payload.get("full_name"),
        "owner": owner.get("login"),
        "url": payload.get("html_url"),
        "visibility": payload.get("visibility"),
        "default_branch": payload.get("default_branch"),
    }


async def _find_existing_repo(
    connector: GitHubRestConnector,
    target: Any,
    operator: Operator,
    org: str,
    name: str,
) -> dict[str, Any] | None:
    """Return the repository ``org/name`` when it exists, else ``None`` (404)."""
    path = await connector.mount_op_path(target, _REPO_PATH.format(org=org, name=name), operator)
    try:
        payload = await connector._get_json(target, path, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"repo_create: payload from {path!r} for {org}/{name} is not an object; "
            f"got {type(payload).__name__}"
        )
    return payload


async def repo_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Create a repository in an organization via ``POST /orgs/{org}/repos``.

    Op-id: ``gh.composite.repo_create``. The repository is private
    unless ``visibility`` is ``"public"``. ``with_readme=True`` asks
    GitHub for a first commit with an empty README, so the default
    branch exists at once.

    Returns
    -------
    dict[str, Any]
        ``{"status": "created" | "unchanged", "name", "full_name",
        "owner", "url", "visibility", "default_branch", "guidance"}``.
        ``"unchanged"`` means a repository with that name already
        exists; nothing was written and the fields describe the
        existing repository.

    Raises
    ------
    httpx.HTTPStatusError
        A non-2xx status other than a 404 on the existence read -- 403
        (the identity may not create repositories in this organization)
        or 422 (GitHub rejected the name or a setting). Propagates for
        the dispatcher's outer branch to map to a structured result.
    RuntimeError
        A payload was not a JSON object.
    ValueError
        ``org`` or ``name`` is not a safe GitHub name. Raised before any
        request is sent.
    """
    org = _checked_path_part("org", params["org"])
    name = _checked_path_part("name", params["name"])
    visibility = params.get("visibility", "private")
    with_readme = bool(params.get("with_readme", False))
    description = params.get("description")

    existing = await _find_existing_repo(connector, target, operator, org, name)
    if existing is not None:
        return {
            "status": "unchanged",
            **_project_created_repo(existing),
            "guidance": (
                f"a repository named {name!r} already exists in organization {org!r}; "
                "nothing was written"
            ),
        }

    body: dict[str, Any] = {
        "name": name,
        "visibility": visibility,
        "private": visibility == "private",
        "auto_init": with_readme,
    }
    if description:
        body["description"] = description

    path = await connector.mount_op_path(target, _ORG_REPOS_PATH.format(org=org), operator)
    created = await connector._post_json(target, path, operator=operator, verb="POST", json=body)
    if not isinstance(created, dict):
        raise RuntimeError(
            f"repo_create: payload from {path!r} for {org}/{name} is not an object; "
            f"got {type(created).__name__}"
        )
    return {
        "status": "created",
        **_project_created_repo(created),
        "guidance": f"repository {org}/{name} was created with visibility {visibility!r}",
    }
