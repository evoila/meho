# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 value types for the add-on pairing surface (#3025).

Intake (:class:`PairAddonRequest`) forbids unknown fields so a typo in the
handshake body is a 422, not a silently-dropped version claim. The read
model (:class:`PairedAddonRead`) is built from the ORM row via
``model_validate``. :class:`PairAddonResult` is the one-time pairing
response: it carries the freshly-minted ``client_secret`` the add-on needs
to authenticate as its service principal — returned exactly once, never
persisted in plaintext, never re-readable through the list/get surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.auth.runner_principals import NAME_MAX_LENGTH

__all__ = [
    "PairAddonRequest",
    "PairAddonResult",
    "PairedAddonListResponse",
    "PairedAddonRead",
]


class PairAddonRequest(BaseModel):
    """Intake for :meth:`AddonPairingService.pair` — the handshake body.

    ``addon_contract_version`` is the contract version the add-on speaks;
    ``addon_min_backplane_version`` is the oldest backplane contract the
    add-on will accept. Both drive the bidirectional negotiation in
    :mod:`meho_backplane.operations.addon_pairing_contract`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=NAME_MAX_LENGTH)
    owner_sub: str | None = None
    addon_contract_version: int = Field(ge=1)
    addon_min_backplane_version: int = Field(ge=1)


class PairedAddonRead(BaseModel):
    """Row representation returned by list / get / heartbeat accessors."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    keycloak_client_id: str
    owner_sub: str
    contract_version: int
    addon_contract_version: int
    addon_min_backplane_version: int
    created_by_sub: str
    paired_at: datetime
    last_seen_at: datetime | None
    updated_at: datetime


class PairAddonResult(BaseModel):
    """One-time pairing response carrying the add-on's fresh credentials.

    ``client_secret`` is repr-hidden so it never lands in a log line or an
    exception render; it is serialised in the HTTP body once (the handshake
    response) and is unrecoverable afterwards.
    """

    model_config = ConfigDict(frozen=True)

    pairing: PairedAddonRead
    client_id: str
    client_secret: str = Field(repr=False)
    backplane_contract_version: int
    negotiated_contract_version: int


class PairedAddonListResponse(BaseModel):
    """Unified list envelope (api-shape-conventions §2) for paired add-ons."""

    model_config = ConfigDict(frozen=True)

    items: list[PairedAddonRead]
    next_cursor: str | None = None
