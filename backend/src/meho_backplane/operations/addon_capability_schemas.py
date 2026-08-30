# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic v2 value types for add-on capability advertisement (#3026).

A paired add-on *declares* the surfaces it contributes to the backplane —
meta-tool families, CLI verb families, console panels, event kinds — against
the integration-contract version it negotiated at pairing time (#3025). The
backplane persists the declaration and activates those surfaces only while
the pairing is present **and** contract-healthy.

Two things are versioned with the contract here:

* **The capability vocabulary** — :class:`CapabilityKind` is the set of
  surface kinds the *current* contract understands. A declaration naming a
  kind outside it is rejected loudly (a 422 at the REST boundary), never a
  silently-dropped surface. Growing the vocabulary is a coordinated
  code + migration change (the kind list is mirrored by the
  ``addon_capability.kind`` CHECK constraint and
  :data:`meho_backplane.db.models.ADDON_CAPABILITY_KINDS`).
* **The declaration itself** — every persisted capability records the
  ``declared_contract_version`` it was advertised against (the pairing's
  negotiated version), so a declaration made against an older contract stays
  distinguishable after the backplane's contract advances.

Intake models forbid unknown fields (``extra="forbid"``) so a typo in the
handshake body is a 422, not a silently-ignored key.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CAPABILITY_NAME_MAX_LENGTH",
    "ActiveCapabilityRead",
    "CapabilityDeclaration",
    "CapabilityDeclarationResponse",
    "CapabilityKind",
    "CapabilityRead",
    "DeclareCapabilitiesRequest",
]

#: Upper bound on a capability ``name`` / ``display_label``. Generous enough
#: for a dotted event kind (``run.step.completed``) or a hyphenated panel id,
#: bounded so a declaration can't smuggle an unbounded blob into a Text column.
CAPABILITY_NAME_MAX_LENGTH: int = 128

#: Identifier alphabet for a capability ``name``: letters, digits, and the
#: punctuation real surface identifiers use (dot for event kinds, colon /
#: slash / hyphen / underscore for namespaced families and panel ids). No
#: whitespace — a name with a space is a malformed declaration, rejected loud.
_NAME_PATTERN: str = r"^[A-Za-z0-9._:/-]+$"


class CapabilityKind(StrEnum):
    """The surface kinds a paired add-on may advertise under contract v1.

    A ``str`` enum so it serialises as its wire value and an unknown kind on
    the request path fails Pydantic validation with a 422 that names the
    offending value — the "rejected loudly" contract. The vocabulary is
    versioned with the integration contract: adding a kind is a coordinated
    change across this enum, the ``addon_capability.kind`` CHECK constraint,
    and :data:`meho_backplane.db.models.ADDON_CAPABILITY_KINDS`.
    """

    META_TOOL_FAMILY = "meta_tool_family"
    CLI_VERB_FAMILY = "cli_verb_family"
    CONSOLE_PANEL = "console_panel"
    EVENT_KIND = "event_kind"


class CapabilityDeclaration(BaseModel):
    """One advertised surface — a ``(kind, name)`` pair plus an optional label.

    ``name`` is the surface identifier within its kind (a meta-tool family
    name, a CLI verb family, a console panel id, an event kind). ``kind``
    outside :class:`CapabilityKind` is a 422; a malformed ``name`` (spaces,
    control chars) is likewise rejected.
    """

    model_config = ConfigDict(extra="forbid")

    kind: CapabilityKind
    name: str = Field(min_length=1, max_length=CAPABILITY_NAME_MAX_LENGTH, pattern=_NAME_PATTERN)
    display_label: str | None = Field(default=None, max_length=CAPABILITY_NAME_MAX_LENGTH)


class DeclareCapabilitiesRequest(BaseModel):
    """Replace-all intake for the capability declaration (the full surface set).

    A declaration is the add-on's *complete* current surface set: persisting
    it replaces any prior declaration wholesale, so a capability dropped from
    the list is deactivated and leaves no dead surface. A ``(kind, name)``
    listed twice is a malformed declaration and is rejected loudly rather
    than silently de-duplicated.
    """

    model_config = ConfigDict(extra="forbid")

    capabilities: list[CapabilityDeclaration]

    @model_validator(mode="after")
    def _reject_duplicate_capabilities(self) -> DeclareCapabilitiesRequest:
        seen: set[tuple[str, str]] = set()
        for cap in self.capabilities:
            key = (cap.kind.value, cap.name)
            if key in seen:
                raise ValueError(
                    f"duplicate capability declared: kind={cap.kind.value!r} name={cap.name!r}"
                )
            seen.add(key)
        return self


class CapabilityRead(BaseModel):
    """One persisted capability row, built from the ORM via ``model_validate``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: CapabilityKind
    name: str
    display_label: str | None
    declared_contract_version: int


class CapabilityDeclarationResponse(BaseModel):
    """The declared surface set for one add-on plus its live activation state.

    ``active`` is derived, never stored: it is ``True`` only while the owning
    pairing is present **and** contract-healthy (re-evaluated live via
    :func:`meho_backplane.operations.addon_pairing_contract.is_contract_compatible`),
    so it flips with the pairing's health without any surface being written
    twice. ``declared_contract_version`` is the pairing's negotiated version
    the declaration was advertised against.
    """

    model_config = ConfigDict(frozen=True)

    addon: str
    declared_contract_version: int
    active: bool
    capabilities: list[CapabilityRead]


class ActiveCapabilityRead(BaseModel):
    """One active capability, tagged with its owning add-on.

    The unit of the tenant-wide *activation* view: a capability whose pairing
    is paired and contract-healthy. This is the plumbing downstream surfaces
    (event push, console) read to learn what is live for a tenant.
    """

    model_config = ConfigDict(frozen=True)

    addon: str
    kind: CapabilityKind
    name: str
    display_label: str | None
