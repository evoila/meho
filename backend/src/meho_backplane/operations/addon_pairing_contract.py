# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Add-on pairing contract — version constants + bidirectional negotiation (#3025).

The pairing handshake binds a sibling add-on product (first consumers:
meho-automation, meho-ssp) to the backplane over a *versioned integration
contract*. This module is the single source of truth for the backplane's
contract version and the pure negotiation logic; the service layer
(:mod:`meho_backplane.operations.addon_pairing`) applies it at pair time
and the health surface re-applies it to persisted pairings.

Minimum-version pinning is enforced in **both** directions:

* **add-on-too-old** — the add-on advertises a contract version below the
  oldest the backplane still accepts
  (:data:`MIN_SUPPORTED_ADDON_CONTRACT_VERSION`). Pairing is refused with
  code ``addon_contract_below_backplane_minimum``.
* **backplane-too-old** — the backplane's own contract version is below the
  minimum the add-on requires (``addon_min_backplane_version``). Pairing is
  refused with code ``backplane_contract_below_addon_minimum``.

When both floors are satisfied the negotiated version is the lower of the
two advertised versions — each side must speak it — and is persisted on the
pairing row.

Versions are monotonic integers, not semver: the contract is a single
in-house surface with one publisher (this backplane) and a small set of
first-party consumers, so an ordered counter carries all the pinning
semantics without the parsing surface of a dotted string.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BACKPLANE_CONTRACT_VERSION",
    "MIN_SUPPORTED_ADDON_CONTRACT_VERSION",
    "ContractSkewError",
    "NegotiatedContract",
    "is_contract_compatible",
    "negotiate",
]

#: The integration-contract version this backplane build speaks. Bump when
#: the pairing contract's observable shape changes in a way a paired add-on
#: must adapt to. The first shipped contract is ``1``.
BACKPLANE_CONTRACT_VERSION: int = 1

#: The oldest add-on contract version this backplane still accepts at pair
#: time — the backplane's side of minimum-version pinning. Raise it only in
#: a release that intentionally drops support for an older add-on contract;
#: raising it can turn a previously-healthy persisted pairing incompatible
#: (surfaced by :func:`is_contract_compatible`).
MIN_SUPPORTED_ADDON_CONTRACT_VERSION: int = 1


class ContractSkewError(Exception):
    """Raised when the add-on and backplane contract versions cannot pair.

    Carries a stable machine ``code`` the REST/console surfaces render
    verbatim, plus the two diagnostic versions for operator triage. The
    two codes name the direction of the skew so an operator knows which
    side to upgrade.
    """

    def __init__(
        self,
        code: str,
        *,
        message: str,
        addon_contract_version: int,
        addon_min_backplane_version: int,
    ) -> None:
        self.code = code
        self.message = message
        self.addon_contract_version = addon_contract_version
        self.addon_min_backplane_version = addon_min_backplane_version
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class NegotiatedContract:
    """The outcome of a successful :func:`negotiate`.

    ``negotiated_version`` is what both sides operate on; the three inputs
    are retained so the pairing row records exactly what each side brought
    to the handshake (enabling :func:`is_contract_compatible` to re-evaluate
    skew after a backplane upgrade).
    """

    negotiated_version: int
    backplane_contract_version: int
    addon_contract_version: int
    addon_min_backplane_version: int


def negotiate(
    *,
    addon_contract_version: int,
    addon_min_backplane_version: int,
) -> NegotiatedContract:
    """Negotiate the contract version for a pairing, pinning both directions.

    Raises :class:`ContractSkewError` when either minimum-version floor is
    violated. On success returns the :class:`NegotiatedContract` whose
    ``negotiated_version`` is ``min(addon_contract_version,
    BACKPLANE_CONTRACT_VERSION)`` — the highest version both sides speak.
    """
    if addon_contract_version < MIN_SUPPORTED_ADDON_CONTRACT_VERSION:
        raise ContractSkewError(
            "addon_contract_below_backplane_minimum",
            message=(
                f"add-on contract v{addon_contract_version} is below the "
                f"backplane minimum v{MIN_SUPPORTED_ADDON_CONTRACT_VERSION}; "
                "upgrade the add-on"
            ),
            addon_contract_version=addon_contract_version,
            addon_min_backplane_version=addon_min_backplane_version,
        )
    if addon_min_backplane_version > BACKPLANE_CONTRACT_VERSION:
        raise ContractSkewError(
            "backplane_contract_below_addon_minimum",
            message=(
                f"backplane contract v{BACKPLANE_CONTRACT_VERSION} is below "
                f"the add-on's required minimum v{addon_min_backplane_version}; "
                "upgrade the backplane"
            ),
            addon_contract_version=addon_contract_version,
            addon_min_backplane_version=addon_min_backplane_version,
        )
    return NegotiatedContract(
        negotiated_version=min(addon_contract_version, BACKPLANE_CONTRACT_VERSION),
        backplane_contract_version=BACKPLANE_CONTRACT_VERSION,
        addon_contract_version=addon_contract_version,
        addon_min_backplane_version=addon_min_backplane_version,
    )


def is_contract_compatible(
    *,
    addon_contract_version: int,
    addon_min_backplane_version: int,
) -> bool:
    """Return whether a persisted pairing still satisfies the current contract.

    The health signal for a pairing: a version pair that negotiated cleanly
    against an earlier build can drift incompatible after an upgrade that
    raised :data:`MIN_SUPPORTED_ADDON_CONTRACT_VERSION` past the add-on's
    advertised version, or a downgrade that dropped
    :data:`BACKPLANE_CONTRACT_VERSION` below the add-on's required minimum.
    Both directions are checked, mirroring :func:`negotiate`, so the surface
    stays truthful across backplane version changes without re-running the
    handshake.
    """
    if addon_contract_version < MIN_SUPPORTED_ADDON_CONTRACT_VERSION:
        return False
    return addon_min_backplane_version <= BACKPLANE_CONTRACT_VERSION
