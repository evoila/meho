# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The connector package set of this process, and its digest (#5038).

A MEHO image can carry connector packages next to the built-in connectors.
The allowlist file lists them
(:data:`~meho_backplane.connector_packages.CONNECTOR_PACKAGES_FILE`). A
satellite runner may run another image than the central MEHO, so it may
lack a package whose operation the central MEHO assigns to it.

The **package set digest** names the set of connector packages an image
carries: ``sha256:`` and the SHA-256 hex digest over the sorted
``(name, version, wheel digest)`` entries of the allowlist. The plain image
has no allowlist file, so its digest is :data:`EMPTY_PACKAGE_SET_DIGEST`.

Both sides use it:

* the runner sends its digest on every call in :data:`PACKAGE_SET_HEADER`,
  next to its wire version;
* the central MEHO puts its own digest on each work item for an operation
  that a connector package provides, and does not assign such an item to a
  runner whose digest differs;
* the runner refuses an item whose digest differs from its own.

See ``docs/codebase/satellite-runner.md``, "Connector package set check".
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from meho_backplane.connector_packages import ConnectorPackage, load_connector_packages

__all__ = [
    "EMPTY_PACKAGE_SET_DIGEST",
    "PACKAGE_SET_DIGEST_PATTERN",
    "PACKAGE_SET_HEADER",
    "PACKAGE_SET_MISMATCH",
    "PackageSet",
    "local_package_set",
    "package_set_digest",
    "package_set_mismatch_message",
]

#: Request header that carries the runner's package set digest.
PACKAGE_SET_HEADER: Final[str] = "X-MEHO-Runner-Package-Set"

#: First word of the refusal text when the package sets differ.
PACKAGE_SET_MISMATCH: Final[str] = "connector_package_set_mismatch"

#: The shape of a digest. The central MEHO refuses a header in any other
#: shape, so the runner list never shows a value a caller made up.
PACKAGE_SET_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}")

_CONNECTOR_MODULE_PREFIX: Final[str] = "meho_backplane.connectors."


def package_set_digest(packages: Iterable[ConnectorPackage]) -> str:
    """Return the digest of a set of connector packages.

    The entries are sorted, so the order in the allowlist file does not
    matter. The folder names are left out: the wheel digest already covers
    the files of a package.
    """
    entries = sorted([p.name, p.version, p.wheel_digest] for p in packages)
    canonical = json.dumps(entries, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: The digest of an image without connector packages (no allowlist file).
#: A runner that sends no digest counts as this: every runner built before
#: the header existed ran an image without connector packages.
EMPTY_PACKAGE_SET_DIGEST: Final[str] = package_set_digest(())


def package_set_mismatch_message(runner_digest: str, needed_digest: str) -> str:
    """Return the refusal text when a runner lacks the package set a work item needs.

    Names both digests, for example ``connector_package_set_mismatch: the
    work item needs connector package set sha256:ab..., the runner has
    sha256:cd...: run the runner on an image with the same connector
    packages as the central MEHO``.
    """
    return (
        f"{PACKAGE_SET_MISMATCH}: the work item needs connector package set {needed_digest}, "
        f"the runner has {runner_digest}: run the runner on an image with the same "
        "connector packages as the central MEHO"
    )


@dataclass(frozen=True)
class PackageSet:
    """The connector packages of this process: their digest and folders."""

    digest: str
    #: Connector folder name -> the name of the package that installed it.
    folders: Mapping[str, str]

    @classmethod
    def of(cls, packages: Iterable[ConnectorPackage]) -> PackageSet:
        packages = list(packages)
        folders = {folder: p.name for p in packages for folder in p.folders}
        return cls(digest=package_set_digest(packages), folders=MappingProxyType(folders))

    def package_for_handler(self, handler_ref: str) -> str | None:
        """The package whose connector folder holds *handler_ref*, or ``None`` (built in)."""
        if not handler_ref.startswith(_CONNECTOR_MODULE_PREFIX):
            return None
        folder = handler_ref.removeprefix(_CONNECTOR_MODULE_PREFIX).split(".", 1)[0]
        return self.folders.get(folder)


@functools.cache
def local_package_set() -> PackageSet:
    """The connector package set of this image, read once from the allowlist file.

    The start-up folder check
    (:func:`~meho_backplane.connector_packages.check_connector_folders`)
    has already read and checked the same file when the connectors were
    imported, on the central MEHO and on the runner.
    """
    return PackageSet.of(load_connector_packages())
