# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Start-up check of the connector folders before MEHO imports them.

MEHO finds connectors by walking ``meho_backplane/connectors/`` at start
and importing every subpackage there. Route modules and other code also
import single connector subpackages directly, long before the walk. So the
check runs in the ``__init__`` of the ``connectors`` package itself: Python
runs that before any ``connectors.<folder>`` module, whoever imports it.

This module lives next to the ``connectors`` package, not inside it. A
folder added under ``connectors/`` with the same name as a module there
takes its place on import (Python prefers a package folder over a module
file), so a module inside the tree could be replaced by the very thing it
has to check. Nothing under ``connectors/`` can shadow a module of
``meho_backplane`` itself.

The check looks at where each subpackage folder came from. A folder is
allowed when:

* its files belong to the ``meho-backplane`` distribution (the folder came
  with MEHO itself), or
* it is named in the allowlist file and its files belong to the
  distribution that the allowlist entry names.

Any other folder stops the start with ``connector_package_not_allowed``.
Nothing is imported in that case.

The allowlist file (:data:`CONNECTOR_PACKAGES_FILE`) is written by the
build of an overlay image. The plain public image has none, so only the
built-in folders are allowed there.

The ``connectors`` package records the allowed packages
(:func:`record_installed_connector_packages`). Operation registration
then marks each row a package writes with that package's name
(:func:`connector_package_of`), and discovery and dispatch hide rows whose
package is not installed (:func:`installed_connector_packages`).

"Files belong to a distribution" is read from the installed file list of
that distribution (its ``RECORD``, through :mod:`importlib.metadata`).
Every file on disk in the folder must be in that list; ``__pycache__``
folders are skipped. A symbolic link anywhere in the folder, or the folder
being a link itself, is never allowed: a wheel cannot contain links, and
a link can point at files that are not in the list. An editable install
(a source checkout set up with ``uv sync``) has no such list for its source
files, so for it every folder inside the checkout counts as built in.

See ``docs/codebase/connector-packages.md``.
"""

from __future__ import annotations

import json
import os
import pkgutil
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Annotated, Final
from urllib.parse import unquote, urlsplit

import structlog
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

__all__ = [
    "BUILTIN_DISTRIBUTION",
    "CONNECTOR_PACKAGES_FILE",
    "ConnectorPackage",
    "ConnectorPackageError",
    "check_connector_folders",
    "connector_package_of",
    "installed_connector_packages",
    "load_connector_packages",
    "record_installed_connector_packages",
]

_log = structlog.get_logger(__name__)

CONNECTOR_PACKAGES_FILE: Path = Path("/app/connector-packages.json")
"""The allowlist file. The overlay image build writes it; the public image has none."""

BUILTIN_DISTRIBUTION: str = "meho-backplane"
"""The distribution whose connector folders are built in."""

_PYCACHE: Final[str] = "__pycache__"

_CONNECTORS_MODULE_PREFIX: Final[str] = "meho_backplane.connectors."

#: Connector folder -> name of the listed package that installed it. Set
#: once, when :mod:`meho_backplane.connectors` is imported. Built-in
#: folders are not in it.
_installed_folders: dict[str, str] = {}


class ConnectorPackageError(RuntimeError):
    """MEHO refuses to start: a connector folder is not allowed, or the allowlist is wrong."""


class ConnectorPackage(BaseModel):
    """One connector package named in the allowlist file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    wheel_digest: str = Field(min_length=1)
    folders: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]] = Field(
        min_length=1
    )


class _ConnectorPackageList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packages: list[ConnectorPackage]


def load_connector_packages(path: Path | None = None) -> list[ConnectorPackage]:
    """Read the allowlist file. A missing file means no extra packages.

    Raises :class:`ConnectorPackageError` (``connector_package_list_invalid``)
    when the file cannot be read or does not have the expected shape, or
    when two entries claim the same folder.
    """
    path = CONNECTOR_PACKAGES_FILE if path is None else path
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConnectorPackageError(
            f"connector_package_list_invalid: MEHO cannot read the connector package list "
            f"{path}: {exc}"
        ) from exc
    try:
        packages = _ConnectorPackageList.model_validate(json.loads(raw)).packages
    except (ValueError, ValidationError) as exc:
        raise ConnectorPackageError(
            f"connector_package_list_invalid: the connector package list {path} is not valid: {exc}"
        ) from exc
    seen: dict[str, str] = {}
    for package in packages:
        for folder in package.folders:
            if folder in seen:
                raise ConnectorPackageError(
                    f"connector_package_list_invalid: the folder {folder!r} is listed for both "
                    f"{seen[folder]!r} and {package.name!r} in {path}."
                )
            seen[folder] = package.name
    return packages


def _editable_root(dist: metadata.Distribution) -> Path | None:
    """The source folder of an editable install (PEP 610 ``direct_url.json``), else ``None``."""
    text = dist.read_text("direct_url.json")
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    dir_info = data.get("dir_info") if isinstance(data, dict) else None
    if not isinstance(dir_info, dict) or dir_info.get("editable") is not True:
        return None
    url = urlsplit(str(data.get("url", "")))
    if url.scheme != "file":
        return None
    return Path(unquote(url.path)).resolve()


class _Installed:
    """The files one distribution installed in the connector folders."""

    def __init__(self, dist: metadata.Distribution, connectors_dir: Path) -> None:
        self.root = Path(str(dist.locate_file(""))).resolve()
        self.owned: dict[str, set[PurePosixPath]] = {}
        try:
            prefix = connectors_dir.relative_to(self.root).parts
        except ValueError:
            return
        for file in dist.files or []:
            parts = PurePosixPath(file).parts
            if (
                len(parts) > len(prefix) + 1
                and parts[: len(prefix)] == prefix
                and _PYCACHE not in parts
            ):
                self.owned.setdefault(parts[len(prefix)], set()).add(PurePosixPath(*parts))

    def owns(self, connectors_dir: Path, folder: str) -> bool:
        """Whether the folder exists and every file in it is in the list.

        ``__pycache__`` folders are skipped. Any symbolic link makes the
        answer ``False``: ``os.walk`` does not enter a linked folder, so
        its files would never be checked, and a linked file can point
        anywhere.
        """
        owned = self.owned.get(folder)
        path = connectors_dir / folder
        if not owned or path.is_symlink() or not path.is_dir():
            return False
        for current, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != _PYCACHE]
            here = Path(current)
            if any((here / d).is_symlink() for d in dirs):
                return False
            for name in files:
                file = here / name
                if (
                    file.is_symlink()
                    or PurePosixPath(file.relative_to(self.root).as_posix()) not in owned
                ):
                    return False
        return True


def _listed_folders(
    packages: list[ConnectorPackage], connectors_dir: Path, path: Path
) -> dict[str, str]:
    """Check each listed package against what is installed; map folder -> package name."""
    listed: dict[str, str] = {}
    for package in packages:
        try:
            dist = metadata.distribution(package.name)
        except metadata.PackageNotFoundError:
            raise ConnectorPackageError(
                f"connector_package_list_invalid: the connector package {package.name!r} is listed "
                f"in {path} but is not installed."
            ) from None
        if dist.version != package.version:
            raise ConnectorPackageError(
                f"connector_package_list_invalid: the connector package {package.name!r} is listed "
                f"in {path} with version {package.version!r}, but version {dist.version!r} "
                "is installed."
            )
        installed = _Installed(dist, connectors_dir)
        for folder in package.folders:
            if not installed.owns(connectors_dir, folder):
                raise ConnectorPackageError(
                    f"connector_package_not_allowed: the connector folder {folder!r} is listed for "
                    f"{package.name!r} in {path}, but its files were not all installed by that "
                    "package, or it holds a link. MEHO does not import it."
                )
            listed[folder] = package.name
    return listed


def check_connector_folders(
    connectors_dir: Path, allowlist_path: Path | None = None
) -> dict[str, str]:
    """Stop the start unless every subpackage folder in ``connectors_dir`` is built in or listed.

    Called from the ``__init__`` of the ``connectors`` package, so it runs
    before any connector subpackage is imported. Raises
    :class:`ConnectorPackageError` that names the first folder (in name
    order) that is not allowed. On success, logs one
    ``connector_packages_allowed`` line and returns the listed folders,
    each mapped to the name of its package.
    """
    path = CONNECTOR_PACKAGES_FILE if allowlist_path is None else allowlist_path
    connectors_dir = connectors_dir.resolve()
    folders = sorted(
        name for _, name, ispkg in pkgutil.iter_modules([str(connectors_dir)]) if ispkg
    )
    packages = load_connector_packages(path)
    listed = _listed_folders(packages, connectors_dir, path)
    builtin: _Installed | None = None
    editable_root: Path | None = None
    try:
        dist = metadata.distribution(BUILTIN_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        pass
    else:
        editable_root = _editable_root(dist)
        if editable_root is None:
            builtin = _Installed(dist, connectors_dir)

    for folder in folders:
        if folder in listed:
            continue
        if editable_root is not None and (connectors_dir / folder).is_relative_to(editable_root):
            continue
        if builtin is not None and builtin.owns(connectors_dir, folder):
            continue
        raise ConnectorPackageError(
            f"connector_package_not_allowed: the connector folder {folder!r} in {connectors_dir}, "
            f"or a file or link in it, did not come with MEHO and is not listed in {path}. MEHO "
            "does not import it. Remove what was added, or build the image with its package listed."
        )

    _log.info(
        "connector_packages_allowed",
        packages=[
            {
                "name": p.name,
                "version": p.version,
                "wheel_digest": p.wheel_digest,
                "folders": p.folders,
            }
            for p in packages
        ],
        source_checkout=editable_root is not None,
    )
    return listed


def record_installed_connector_packages(folders: Mapping[str, str]) -> None:
    """Remember the listed folders and their packages for this process.

    *folders* is what :func:`check_connector_folders` returned. The
    ``connectors`` package calls this once, right after the check.
    """
    _installed_folders.clear()
    _installed_folders.update(folders)


def installed_connector_packages() -> frozenset[str]:
    """Names of the connector packages that are installed and allowed in this process."""
    return frozenset(_installed_folders.values())


def connector_package_of(dotted_path: str) -> str | None:
    """The installed package that owns *dotted_path*, or ``None`` for built-in code.

    *dotted_path* is a module path or a ``handler_ref``. It belongs to a
    package when it lies in a connector folder that the package installed.
    """
    if not dotted_path.startswith(_CONNECTORS_MODULE_PREFIX):
        return None
    folder = dotted_path[len(_CONNECTORS_MODULE_PREFIX) :].split(".", 1)[0]
    return _installed_folders.get(folder)
