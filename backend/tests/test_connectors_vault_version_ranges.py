# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Vault connector declares one range for HashiCorp Vault and one for OpenBao.

Both products share the product slug ``vault`` and the same operations. The
resolver picks the class by the version of the target. OpenBao 2.x and
HashiCorp Vault 2.x use the same numbers, so Vault 2.x gets no connector
until a CI run on it is green.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.connectors.resolver import NoMatchingConnector, resolve_connector
from meho_backplane.connectors.vault import OpenBaoConnector, VaultConnector


def _target(version: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        product="vault",
        name="t",
        version=None,
        fingerprint=SimpleNamespace(version=version),
        preferred_impl_id=None,
        tenant_id=None,
    )


@pytest.fixture(autouse=True)
def _registered() -> None:
    _eager_import_connectors()


def test_ranges_are_declared() -> None:
    assert VaultConnector.supported_version_range == ">=1.18,<2.0"
    assert OpenBaoConnector.supported_version_range == ">=2.0,<3.0"


@pytest.mark.parametrize("version", ["1.18.0", "1.18.5", "1.21.1"])
def test_vault_1_x_resolves_to_vault(version: str) -> None:
    assert resolve_connector(_target(version)) is VaultConnector


@pytest.mark.parametrize("version", ["2.0.0", "2.7.1", "2.99.0"])
def test_openbao_2_x_resolves_to_openbao(version: str) -> None:
    assert resolve_connector(_target(version)) is OpenBaoConnector


def test_unknown_version_still_resolves() -> None:
    assert resolve_connector(_target(None)) is VaultConnector


@pytest.mark.parametrize("version", ["1.17.6", "1.0.0", "3.0.0"])
def test_versions_outside_both_ranges_get_no_connector(version: str) -> None:
    with pytest.raises(NoMatchingConnector):
        resolve_connector(_target(version))


def test_a_probe_still_works_on_a_version_outside_the_range() -> None:
    assert resolve_connector(_target("1.17.6"), for_probe=True) is VaultConnector
