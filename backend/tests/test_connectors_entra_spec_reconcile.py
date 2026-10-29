# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Spec path check of the Entra ID connector.

Every Microsoft Graph path the Entra ID connector calls is a ``*_PATH``
constant in :mod:`meho_backplane.connectors.entra._paths`. This check fails
when one of them is not in Microsoft's own Graph v1.0 API description
(``openapi/v1.0/openapi.yaml`` in ``microsoftgraph/msgraph-metadata``, MIT
license).

That file is about 44 MB, so it is not on the spec shelf yet. Until it is,
the compare test skips, like every spec check when its file is missing
(``tests/_spec_shelf.py``). The other tests here run always: they keep the
list of checked paths and their HTTP methods in step with ``_paths.py``. The
evidence and what turns the compare on are in the ``entra-graph-1.0`` entry
of ``docs/decisions/spec-reconcile-guards-standard.md``.

How paths are compared:

* The spec's ``servers[0].url`` ends in ``/v1.0`` and its paths do not, so
  the check puts that prefix in front of every spec path, the same way the
  connector's constants carry it.
* Parameter names are ignored. Graph names them ``{group-id}``; the
  connector names them ``{group_id}``. Every ``{...}`` part compares as
  ``{}``.

A red run is the check finding a real problem; see the standard above.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from meho_backplane.connectors.entra import _paths
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_SPEC_DIR = "entra-graph-1.0"
_SPEC_FILE = "openapi.yaml"

#: The HTTP methods of an OpenAPI path item. Other keys (``description``,
#: ``parameters``, ``x-ms-...``) are not operations.
_OPENAPI_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

#: The HTTP method the connector sends on each path constant. A path string
#: carries no method, so it is written here. Every Entra ID call is a read.
#: A new ``*_PATH`` constant fails the check until it has a line here.
_METHOD_BY_CONSTANT: dict[str, str] = {
    "APPLICATIONS_PATH": "GET",
    "GROUPS_PATH": "GET",
    "GROUP_MEMBER_COUNT_PATH": "GET",
    "ORGANIZATION_PATH": "GET",
    "ROLE_ASSIGNMENTS_PATH": "GET",
    "ROLE_DEFINITIONS_PATH": "GET",
    "SERVICE_PRINCIPALS_PATH": "GET",
    "USERS_PATH": "GET",
}

_PARAMETER = re.compile(r"\{[^}]*\}")


def _normalise(path: str) -> str:
    """Blank every parameter name, so ``{group-id}`` and ``{group_id}`` match."""
    return _PARAMETER.sub("{}", path)


def _path_constants() -> dict[str, str]:
    """The ``*_PATH`` constants of the Entra ID ``_paths`` module, by name."""
    return {
        name: value
        for name, value in vars(_paths).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every ``METHOD:/path`` the Entra ID connector sends."""
    declared: set[str] = set()
    for name, path in _path_constants().items():
        method = _METHOD_BY_CONSTANT.get(name)
        if method is None:
            raise AssertionError(
                f"entra._paths.{name} has no HTTP method in _METHOD_BY_CONSTANT. "
                "Add it there, so the spec check covers the new path."
            )
        declared.add(f"{method}:{_normalise(path)}")
    return declared


def _served_op_ids(document: Any) -> set[str]:
    """Every ``METHOD:/v1.0/path`` the Graph OpenAPI file describes.

    A file without a server URL or without paths fails the check, so a
    changed or broken file cannot make the check pass without comparing
    anything.
    """
    if not isinstance(document, dict):
        raise AssertionError(f"{_SPEC_DIR}/{_SPEC_FILE}: not an OpenAPI document")
    servers = document.get("servers") or []
    if not servers or not isinstance(servers[0], dict) or not servers[0].get("url"):
        raise AssertionError(f"{_SPEC_DIR}/{_SPEC_FILE}: no servers[0].url")
    base = urlsplit(str(servers[0]["url"])).path.rstrip("/")
    items = document.get("paths") or {}
    if not isinstance(items, dict) or not items:
        raise AssertionError(f"{_SPEC_DIR}/{_SPEC_FILE}: no paths")
    return {
        f"{method.upper()}:{_normalise(base + path)}"
        for path, item in items.items()
        if isinstance(item, dict)
        for method in item
        if method.lower() in _OPENAPI_METHODS
    }


def test_every_path_constant_has_a_method() -> None:
    """A new ``*_PATH`` constant must get a line in :data:`_METHOD_BY_CONSTANT`."""
    assert sorted(_path_constants()) == sorted(_METHOD_BY_CONSTANT)


def test_the_checked_paths_are_pinned() -> None:
    """The list of checked paths, so a change to it is always on purpose."""
    assert sorted(_declared_op_ids()) == [
        "GET:/v1.0/applications",
        "GET:/v1.0/groups",
        "GET:/v1.0/groups/{}/members/$count",
        "GET:/v1.0/organization",
        "GET:/v1.0/roleManagement/directory/roleAssignments",
        "GET:/v1.0/roleManagement/directory/roleDefinitions",
        "GET:/v1.0/servicePrincipals",
        "GET:/v1.0/users",
    ]


def test_spec_paths_get_the_server_prefix_and_lose_parameter_names() -> None:
    """The compare puts ``/v1.0`` in front and ignores parameter names."""
    document = {
        "servers": [{"url": "https://graph.example.test/v1.0"}],
        "paths": {
            "/users": {"description": "x", "get": {}, "post": {}},
            "/groups/{group-id}/members/$count": {"parameters": [], "get": {}},
        },
    }
    assert _served_op_ids(document) == {
        "GET:/v1.0/users",
        "POST:/v1.0/users",
        "GET:/v1.0/groups/{}/members/$count",
    }


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"paths": {"/users": {"get": {}}}},
        {"servers": [{"url": "https://graph.example.test/v1.0"}], "paths": {}},
    ],
)
def test_a_broken_spec_file_fails_the_check(document: Any) -> None:
    with pytest.raises(AssertionError):
        _served_op_ids(document)


def test_every_path_is_in_the_graph_v1_spec() -> None:
    """Every Entra ID path is in Microsoft's Graph v1.0 OpenAPI file."""
    spec_path: Path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with spec_path.open(encoding="utf-8") as handle:
        document = yaml.load(handle, Loader=loader)
    assert_op_ids_served(
        _declared_op_ids(),
        _served_op_ids(document),
        spec_label=f"{_SPEC_DIR}/{_SPEC_FILE} (Microsoft Graph v1.0)",
    )
