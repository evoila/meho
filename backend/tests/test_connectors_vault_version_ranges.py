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
from packaging.version import Version

from meho_backplane.checks.connector_fit import connector_fit
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.connectors.resolver import NoMatchingConnector, resolve_connector
from meho_backplane.connectors.vault import OpenBaoConnector, VaultConnector


def _target(version: str | None, probed_product: str | None = None) -> SimpleNamespace:
    """A probed target; the probe found OpenBao for 2.0 and up unless told otherwise."""
    if probed_product is None:
        is_openbao = version is not None and Version(version).major >= 2
        probed_product = "openbao" if is_openbao else "vault"
    return SimpleNamespace(
        product="vault",
        name="t",
        version=None,
        fingerprint=SimpleNamespace(version=version, product=probed_product),
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


def test_hashicorp_vault_2_x_gets_no_connector() -> None:
    """HashiCorp Vault 2.0.0 shares OpenBao's numbers but not its class."""
    with pytest.raises(NoMatchingConnector) as caught:
        resolve_connector(_target("2.0.0", probed_product="vault"))
    assert "only openbao" in str(caught.value)
    assert resolve_connector(_target("2.0.0", probed_product="openbao")) is OpenBaoConnector


def test_unprobed_2_x_target_gets_no_connector() -> None:
    """A version typed by hand does not say which product runs there."""
    target = SimpleNamespace(
        product="vault",
        name="t",
        version="2.7.1",
        fingerprint=None,
        preferred_impl_id=None,
        tenant_id=None,
    )
    with pytest.raises(NoMatchingConnector):
        resolve_connector(target)
    assert resolve_connector(target, for_probe=True) is VaultConnector


def test_sensor_on_vault_connector_fits_openbao() -> None:
    """The Vault operations are stored under vault-1.x and run on OpenBao too."""
    assert connector_fit("vault-1.x", _target("2.7.1")).warning is None
    assert connector_fit("vault-1.x", _target("1.18.5")).warning is None


def test_sensor_on_vault_connector_warns_for_hashicorp_2_x() -> None:
    fit = connector_fit("vault-1.x", _target("2.0.0", probed_product="vault"))
    assert fit.warning is not None
    assert fit.matching_connector_id is None
