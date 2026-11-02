# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which installer targets resolve to Cloud Builder, and which to the VCF Installer.

Two implementations of the ``installer`` product are registered the way the
packages register them at start-up:

* ``installer-cb-5.0`` (Cloud Builder, ``>=5.0,<9.0``, no wildcard);
* ``installer-rest-9.1`` (VCF Installer, ``>=9.0,<10.0``) and its
  ``("installer", "", "")`` wildcard.

A Cloud Builder 5.x target resolves to Cloud Builder; a 9.x Installer target
still resolves to ``installer-rest-9.1``; a target with no version yet goes to
the wildcard (``installer-rest``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.resolver import NoMatchingConnector, resolve_connector
from meho_backplane.connectors.vcf_cloud_builder.connector import CloudBuilderConnector
from meho_backplane.connectors.vcf_installer.connector import InstallerConnector


@dataclass
class _Fingerprint:
    version: str | None


@dataclass
class _Target:
    product: str
    fingerprint: _Fingerprint | None = None
    preferred_impl_id: str | None = None
    version: str | None = None


@pytest.fixture(autouse=True)
def _both_installer_impls() -> Iterator[None]:
    clear_registry()
    register_connector_v2(
        product="installer", version="9.1", impl_id="installer-rest", cls=InstallerConnector
    )
    register_connector_v2(product="installer", version="", impl_id="", cls=InstallerConnector)
    register_connector_v2(
        product="installer", version="5.0", impl_id="installer-cb", cls=CloudBuilderConnector
    )
    yield
    clear_registry()


@pytest.mark.parametrize("version", ["5.0.0.0-21822418", "5.1.1.0-23480823", "5.2", "5.2.1.0"])
def test_cloud_builder_target_resolves_to_installer_cb(version: str) -> None:
    target = _Target(product="installer", fingerprint=_Fingerprint(version=version))
    assert resolve_connector(target) is CloudBuilderConnector


def test_operator_set_version_resolves_to_installer_cb() -> None:
    assert resolve_connector(_Target(product="installer", version="5.2")) is CloudBuilderConnector


@pytest.mark.parametrize("version", ["9.0.2.0.25151285", "9.1.0.0.25371088"])
def test_installer_9x_target_still_resolves_to_installer_rest(version: str) -> None:
    target = _Target(product="installer", fingerprint=_Fingerprint(version=version))
    assert resolve_connector(target) is InstallerConnector


def test_target_without_version_goes_to_the_installer_wildcard() -> None:
    assert resolve_connector(_Target(product="installer")) is InstallerConnector


def test_cloud_builder_range_has_no_4x() -> None:
    clear_registry()
    register_connector_v2(
        product="installer", version="5.0", impl_id="installer-cb", cls=CloudBuilderConnector
    )
    target = _Target(product="installer", fingerprint=_Fingerprint(version="4.5.0.0"))
    with pytest.raises(NoMatchingConnector):
        resolve_connector(target)


def test_connector_identity() -> None:
    assert (
        CloudBuilderConnector.product,
        CloudBuilderConnector.version,
        CloudBuilderConnector.impl_id,
        CloudBuilderConnector.supported_version_range,
    ) == ("installer", "5.0", "installer-cb", ">=5.0,<9.0")
