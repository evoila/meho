# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which installer targets resolve to ``installer-rest-9.1``.

The connector advertises ``>=9.0,<10.0``. A VCF 9.0.x bring-up uses Installer
9.0.2, so a 9.0.2 target must resolve here, the same as a 9.1 target. Every
path the connector calls is in the pinned 9.0 installer API file; the 9.0 case
of :mod:`tests.test_connectors_vcf_installer_spec_reconcile` checks that.

The version strings below have the shape ``GET /v1/system/appliance-info``
returns (``<major>.<minor>.<patch>.<n>.<build>``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.resolver import NoMatchingConnector, resolve_connector
from meho_backplane.connectors.vcf_installer.connector import InstallerConnector


@dataclass
class _FakeFingerprint:
    version: str | None


@dataclass
class _FakeTarget:
    product: str
    fingerprint: _FakeFingerprint | None = None
    preferred_impl_id: str | None = None
    version: str | None = None


@pytest.fixture(autouse=True)
def _versioned_entry_only() -> Iterator[None]:
    """Register only the versioned installer entry.

    The package ``__init__`` also registers a ``("installer", "", "")`` wildcard
    for a target with no version yet. The wildcard matches any version, so it
    would hide the range. Without it, a target resolves only when its version
    is inside ``supported_version_range``.
    """
    clear_registry()
    register_connector_v2(
        product="installer", version="9.1", impl_id="installer-rest", cls=InstallerConnector
    )
    yield
    clear_registry()


@pytest.mark.parametrize(
    "version",
    ["9.0.2.0.25151285", "9.0.0.0", "9.1.0.0.25371088", "9.1.1.0.25713928"],
)
def test_9x_installer_target_resolves_installer_rest(version: str) -> None:
    target = _FakeTarget(product="installer", fingerprint=_FakeFingerprint(version=version))
    assert resolve_connector(target) is InstallerConnector


@pytest.mark.parametrize("version", ["8.0.3.0", "10.0.0.0"])
def test_installer_target_outside_9x_has_no_connector(version: str) -> None:
    target = _FakeTarget(product="installer", fingerprint=_FakeFingerprint(version=version))
    with pytest.raises(NoMatchingConnector):
        resolve_connector(target)
