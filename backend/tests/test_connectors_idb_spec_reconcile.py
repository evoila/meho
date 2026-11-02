# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``idb`` hand-written paths: the manifest check (no API file to compare with).

Neither VCF Identity Broker 9.x nor Workspace ONE Access 3.3 offers a
downloadable API file for the versions MEHO covers, so this check is the
manifest half of the standard in
``docs/decisions/spec-reconcile-guards-standard.md``: it reads the ``*_PATH``
constants of ``connectors/idb/_paths.py`` live and pins every
``METHOD:/path`` the two connectors send. A new path, a renamed constant or a
changed method must update this list on purpose, and the reference list in
the ``_paths`` module docstring with it.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.idb import _paths as paths_module

#: HTTP methods per path constant.
_METHODS_BY_CONSTANT: dict[str, tuple[str, ...]] = {
    "WSA_TOKEN_PATH": ("POST",),
    "WSA_HEALTH_PATH": ("GET",),
    "WSA_IDENTITY_PROVIDERS_PATH": ("GET",),
    "WSA_DIRECTORIES_PATH": ("GET",),
    "WSA_DIRECTORY_SYNC_PATH": ("POST",),
    "WSA_USERS_SEARCH_PATH": ("POST",),
    "WSA_GROUPS_SEARCH_PATH": ("POST",),
    "IDB_TOKEN_PATH": ("POST",),
    "IDB_PROPERTIES_PATH": ("GET",),
    "IDB_OIDC_DISCOVERY_PATH": ("GET",),
    "IDB_USERS_SEARCH_PATH": ("POST",),
    "IDB_GROUPS_SEARCH_PATH": ("POST",),
}


def _path_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(paths_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def test_every_path_constant_has_its_methods() -> None:
    assert sorted(_path_constants()) == sorted(_METHODS_BY_CONSTANT)


def test_idb_manifest_is_pinned() -> None:
    declared = {
        f"{method}:{path}"
        for name, path in _path_constants().items()
        for method in _METHODS_BY_CONSTANT[name]
    }
    assert declared == {
        "POST:/SAAS/auth/oauthtoken",
        "GET:/SAAS/API/1.0/REST/system/health",
        "GET:/SAAS/jersey/manager/api/identityProviders",
        "GET:/SAAS/jersey/manager/api/connectormanagement/directoryconfigs",
        "POST:/SAAS/jersey/manager/api/connectormanagement/directoryconfigs/{directory_id}"
        "/syncprofile/sync",
        "POST:/SAAS/jersey/manager/api/scim/Users/.search",
        "POST:/SAAS/jersey/manager/api/scim/Groups/.search",
        "POST:/acs/t/{tenant}/token",
        "GET:/acs/t/{tenant}/properties",
        "GET:/acs/t/{tenant}/.well-known/openid-configuration",
        "POST:/usergroup/t/{tenant}/scim/v2/Users/.search",
        "POST:/usergroup/t/{tenant}/scim/v2/Groups/.search",
    }


def test_reference_list_names_every_path() -> None:
    """Each path is in the reference list of the ``_paths`` docstring."""
    doc = paths_module.__doc__ or ""
    for path in _path_constants().values():
        assert path in doc, f"{path} is not in the reference list of _paths.py"


def test_fill_encodes_one_segment_and_checks_placeholders() -> None:
    assert (
        paths_module.fill(paths_module.WSA_DIRECTORY_SYNC_PATH, {"directory_id": "a/b"})
        == "/SAAS/jersey/manager/api/connectormanagement/directoryconfigs/a%2Fb/syncprofile/sync"
    )
    with pytest.raises(ValueError, match="needs"):
        paths_module.fill(paths_module.IDB_TOKEN_PATH, {})
