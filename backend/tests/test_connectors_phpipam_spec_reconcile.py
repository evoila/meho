# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""phpIPAM hand-written paths: the manifest check (no spec to compare with).

phpIPAM publishes its API as a web page, not as an OpenAPI file, so there is
no file to check the paths against. This check is the manifest half of the
standard in ``docs/decisions/spec-reconcile-guards-standard.md``: it reads
the ``*_PATH`` constants of ``connectors/phpipam/_paths.py`` live and pins
every ``METHOD:/path`` the connector sends. A new path, a renamed constant or
a changed method must update this list on purpose, and the reference list in
the ``_paths`` module docstring with it. The record of what was checked is the
``phpipam-rest-1.x`` entry of the standard's evidenced exclusions.
"""

from __future__ import annotations

from meho_backplane.connectors.phpipam import _paths as paths_module

#: HTTP methods per path constant. ``_USER_PATH`` is the login (POST) and the
#: fingerprint check without a token (GET).
_METHODS_BY_CONSTANT: dict[str, tuple[str, ...]] = {
    "_USER_PATH": ("GET", "POST"),
    "_SUBNETS_PATH": ("GET",),
    "_SUBNET_CIDR_PATH": ("GET",),
    "_SUBNET_PATH": ("GET",),
    "_SUBNET_USAGE_PATH": ("GET",),
    "_SUBNET_SLAVES_PATH": ("GET",),
    "_SUBNET_ADDRESSES_PATH": ("GET",),
    "_SUBNET_ADDRESS_PATH": ("GET",),
    "_ADDRESSES_PATH": ("POST",),
    "_ADDRESS_PATH": ("DELETE",),
    "_FIRST_FREE_PATH": ("GET", "POST"),
}


def _path_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(paths_module).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def test_every_path_constant_has_its_methods() -> None:
    assert sorted(_path_constants()) == sorted(_METHODS_BY_CONSTANT)


def test_phpipam_manifest_is_pinned() -> None:
    declared = {
        f"{method}:{path}"
        for name, path in _path_constants().items()
        for method in _METHODS_BY_CONSTANT[name]
    }
    assert declared == {
        "GET:/api/{app_id}/user/",
        "POST:/api/{app_id}/user/",
        "GET:/api/{app_id}/subnets/",
        "GET:/api/{app_id}/subnets/cidr/{subnet}/{mask}/",
        "GET:/api/{app_id}/subnets/{subnet_id}/",
        "GET:/api/{app_id}/subnets/{subnet_id}/usage/",
        "GET:/api/{app_id}/subnets/{subnet_id}/slaves/",
        "GET:/api/{app_id}/subnets/{subnet_id}/addresses/",
        "GET:/api/{app_id}/subnets/{subnet_id}/addresses/{ip}/",
        "POST:/api/{app_id}/addresses/",
        "DELETE:/api/{app_id}/addresses/{ip}/{subnet_id}/",
        "GET:/api/{app_id}/addresses/first_free/{subnet_id}/",
        "POST:/api/{app_id}/addresses/first_free/{subnet_id}/",
    }


def test_reference_list_names_every_path() -> None:
    """Each path is in the reference list of the ``_paths`` docstring."""
    doc = paths_module.__doc__ or ""
    for path in _path_constants().values():
        assert path in doc, f"{path} is not in the reference list of _paths.py"


def test_release_never_sends_remove_dns() -> None:
    assert "remove_dns" not in paths_module._ADDRESS_PATH
