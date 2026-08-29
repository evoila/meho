# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the add-on pairing contract negotiation (#3025).

Pins the bidirectional minimum-version pinning: an add-on below the
backplane's floor is refused one way, a backplane below the add-on's floor
the other, and a compatible pair negotiates to the lower advertised version.
The health-side re-evaluation (:func:`is_contract_compatible`) mirrors the
same two directions. Boundary inputs derive from the module constants so a
future version bump keeps the tests meaningful.
"""

from __future__ import annotations

import pytest

from meho_backplane.operations.addon_pairing_contract import (
    BACKPLANE_CONTRACT_VERSION,
    MIN_SUPPORTED_ADDON_CONTRACT_VERSION,
    ContractSkewError,
    is_contract_compatible,
    negotiate,
)


def test_negotiate_equal_versions_returns_that_version() -> None:
    result = negotiate(
        addon_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
    )
    assert result.negotiated_version == BACKPLANE_CONTRACT_VERSION
    assert result.backplane_contract_version == BACKPLANE_CONTRACT_VERSION


def test_negotiate_addon_ahead_of_backplane_negotiates_down_to_backplane() -> None:
    """An add-on speaking a newer contract negotiates to what the backplane speaks."""
    result = negotiate(
        addon_contract_version=BACKPLANE_CONTRACT_VERSION + 5,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
    )
    assert result.negotiated_version == BACKPLANE_CONTRACT_VERSION


def test_negotiate_addon_below_backplane_minimum_is_skew() -> None:
    """Direction 1: add-on advertises below the backplane's floor -> refused."""
    with pytest.raises(ContractSkewError) as exc_info:
        negotiate(
            addon_contract_version=MIN_SUPPORTED_ADDON_CONTRACT_VERSION - 1,
            addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
        )
    assert exc_info.value.code == "addon_contract_below_backplane_minimum"


def test_negotiate_backplane_below_addon_minimum_is_skew() -> None:
    """Direction 2: add-on requires a newer backplane than this build -> refused."""
    with pytest.raises(ContractSkewError) as exc_info:
        negotiate(
            addon_contract_version=BACKPLANE_CONTRACT_VERSION,
            addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1,
        )
    assert exc_info.value.code == "backplane_contract_below_addon_minimum"


def test_skew_error_carries_diagnostic_versions() -> None:
    with pytest.raises(ContractSkewError) as exc_info:
        negotiate(
            addon_contract_version=BACKPLANE_CONTRACT_VERSION,
            addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 3,
        )
    err = exc_info.value
    assert err.addon_contract_version == BACKPLANE_CONTRACT_VERSION
    assert err.addon_min_backplane_version == BACKPLANE_CONTRACT_VERSION + 3
    assert err.code in str(err)


def test_is_contract_compatible_true_for_a_negotiable_pair() -> None:
    assert is_contract_compatible(
        addon_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
    )


def test_is_contract_compatible_false_when_addon_below_backplane_minimum() -> None:
    assert not is_contract_compatible(
        addon_contract_version=MIN_SUPPORTED_ADDON_CONTRACT_VERSION - 1,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION,
    )


def test_is_contract_compatible_false_when_backplane_below_addon_minimum() -> None:
    assert not is_contract_compatible(
        addon_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_min_backplane_version=BACKPLANE_CONTRACT_VERSION + 1,
    )
