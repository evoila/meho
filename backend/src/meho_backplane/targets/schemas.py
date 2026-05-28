# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pydantic schemas for the targets surface (G0.3 T2, amended by T1.5).

Four models cover the full CRUD + list contract:

* :class:`Target` — full read shape. Returned by GET /targets/{name}
  and by the resolver. Frozen.
* :class:`TargetSummary` — short shape for list responses. Only the
  fields needed to identify and filter; omits ``notes`` and ``extras``
  to keep list payloads small. Frozen.
* :class:`TargetCreate` — POST body. All required fields explicit; optional
  fields have documented defaults matching the ORM column defaults.
  Rejects unknown fields (``extra='forbid'``).
* :class:`TargetUpdate` — PATCH body. Every field optional; only fields
  that are not ``None`` are applied by the route handler. ``name`` is
  intentionally absent — rename = delete + create. ``product`` is
  patchable as of G0.14-T4 (#1145) with route-handler validation
  against the registered connector products; rejects unknown fields
  (``extra='forbid'``).

The G0.3-T1.5 (#477) amendment added two fields to :class:`Target`:

* ``fingerprint`` — cached
  :class:`~meho_backplane.connectors.schemas.FingerprintResult` from
  the last successful probe. Server-managed: only the probe route
  writes it. **Not** acceptable on :class:`TargetCreate` or
  :class:`TargetUpdate` — both reject ``fingerprint`` with 422 via
  ``extra='forbid'`` so clients cannot seed the G0.6 resolver with
  fabricated values.
* ``preferred_impl_id`` — operator override for the G0.6 resolver's
  tie-break ladder. Acceptable on both write schemas. The canonical
  form is **versioned** (``"nsx-rest-4.2"``) per
  ``docs/codebase/api-shape-conventions.md`` §3 (Enum vocabulary
  discipline); the base form (``"nsx-rest"``) stays accepted on both
  ``TargetCreate`` and ``TargetUpdate`` for backward compatibility,
  and the resolver normalizes both to the same connector
  (G0.16-T6 Finding C #1312).

``AuthModel`` is imported from :mod:`meho_backplane.connectors.schemas`
(G0.2-T1) and re-used here so the enum value set stays in one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.connectors.schemas import AuthModel

__all__ = [
    "AuthModel",
    "Target",
    "TargetCreate",
    "TargetSummary",
    "TargetUpdate",
]


class TargetSummary(BaseModel):
    """Short shape for list endpoints.

    Omits ``notes``, ``extras``, and connection-auth details to keep
    list responses fast and small. The ``aliases`` field is included
    because list consumers (CLI ``meho target list``, autocomplete)
    need it to display secondary names.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    aliases: tuple[str, ...]
    product: str
    host: str


class Target(BaseModel):
    """Full read shape — returned by GET /targets/{name} and the resolver.

    Maps 1:1 to the ``targets`` table columns. Frozen so callers
    can safely stash instances in request state or structured logs
    without fear of mutation.

    ``aliases`` uses ``tuple[str, ...]`` and ``extras`` uses
    ``Mapping[str, Any]`` so frozen instances cannot be mutated in-place
    via list.append / dict.__setitem__ — matching the immutability contract
    the docstring documents.

    ``fingerprint`` mirrors the persisted
    :class:`~meho_backplane.connectors.schemas.FingerprintResult` shape
    (JSON-safe dict from ``model_dump(mode='json')``) or ``None`` until
    the first successful probe. ``preferred_impl_id`` is the operator's
    optional override for the G0.6 connector-impl resolver.

    ``version`` is the operator-asserted product version (e.g.
    ``"9.0"``, ``"1.x"``) shipped by G0.15-T6 (#1215). It is **operator-
    editable** via :class:`TargetCreate` / :class:`TargetUpdate` so a
    fresh target can carry a version *before* the first probe, breaking
    the chicken-and-egg the v0.7.0 dogfood surfaced (RDC #753, signal
    6): every typed connector except K8s required ``fingerprint.version``
    to resolve, but the probe needed the resolver to find a connector
    first. The G0.15-T6 fix adds operator-asserted ``version`` as a
    second source the resolver consults, with ``fingerprint.version``
    (probed reality) taking precedence when both are present. The K8s
    pattern (sibling wildcard registration at
    ``connectors/kubernetes/__init__.py``) is fanned out across every
    typed connector in the same PR so an unfingerprinted target with
    ``version=None`` *also* resolves through the wildcard.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    name: str
    aliases: tuple[str, ...]
    product: str
    # G0.15-T6 (#1215). Defaults to ``None`` so call sites that have
    # not yet been updated to populate the field (test helpers,
    # historical fixtures, legacy code constructing :class:`Target`
    # by hand) keep working; production code paths go through
    # :func:`_to_full` which now passes the column through explicitly.
    version: str | None = None
    host: str
    port: int | None
    fqdn: str | None
    secret_ref: str | None
    auth_model: AuthModel
    vpn_required: bool
    extras: Mapping[str, Any]
    notes: str | None
    fingerprint: Mapping[str, Any] | None
    preferred_impl_id: str | None
    created_at: datetime
    updated_at: datetime
    # Soft-delete timestamp (G0.14-T4 #1145). ``None`` for live
    # targets; a non-``None`` value names the wall-clock time of
    # the ``DELETE /api/v1/targets/{name}`` call that retired the
    # row. Read paths (``resolve_target``, ``list_targets``)
    # exclude rows where the column is non-``None``, so a caller
    # holding a :class:`Target` instance with ``deleted_at`` set
    # observed the target through an audit-history surface, not a
    # live registry probe.
    deleted_at: datetime | None = None


class TargetCreate(BaseModel):
    """POST /api/v1/targets body.

    ``name`` and ``product`` are immutable after creation; to rename a
    target, delete + re-create. ``auth_model`` defaults to
    ``shared_service_account`` matching the DB column default.

    ``fingerprint`` is **not** accepted — it is server-managed and only
    written by the probe handler from the connector's response. Sending
    ``fingerprint`` in the create body raises 422 via ``extra='forbid'``
    so clients cannot seed the G0.6 resolver's tie-break input with
    fabricated values. ``preferred_impl_id`` is accepted as an optional
    operator override.

    ``version`` is accepted as an optional operator-asserted product
    version (G0.15-T6 #1215). Operators who know the version up-front
    (e.g. ``"9.0"`` for a vCenter Hetzner-DC target the consumer just
    deployed) can pass it at create time so the very first probe
    dispatches against the versioned connector without round-tripping
    through PATCH. Fresh targets still default to ``None`` and resolve
    via the sibling wildcard registration applied to every typed
    connector in the same PR (K8s pattern fanned out).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    product: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=100)
    host: str = Field(min_length=1, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    fqdn: str | None = None
    secret_ref: str | None = None
    auth_model: AuthModel = AuthModel.SHARED_SERVICE_ACCOUNT
    vpn_required: bool = False
    extras: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    preferred_impl_id: str | None = Field(default=None, max_length=200)


class TargetUpdate(BaseModel):
    """PATCH /api/v1/targets/{name} body.

    All fields are optional. The route handler applies only the fields
    that are not ``None``; callers must send an explicit ``null`` JSON
    value to clear a nullable column (``fqdn``, ``secret_ref``,
    ``notes``). ``name`` is absent — rename = delete + create.

    ``product`` is patchable as of G0.14-T4 (#1145). The original
    G0.3 contract treated ``product`` as immutable after creation
    on the theory that the operator should delete + re-create on a
    typo, but the v0.6.0 dogfood pass (signal 6) showed the
    combination of "no DELETE route" + "no PATCH on product" left a
    misregistered target permanently broken — name and alias slots
    occupied, ``secret_ref`` pointing at a stranded Vault path. T4
    closes the gap by adding DELETE *and* allowing PATCH on
    ``product``. The route handler validates the new value against
    the set of registered connector products and rejects unknown
    values with a structured 422 mirroring the ``/probe`` 501
    shape — so a typo at PATCH time produces the same actionable
    diagnostic as the typo would at probe time, instead of
    silently breaking the working target.

    ``fingerprint`` is **not** accepted via PATCH — it is server-managed
    and rewritten by every successful probe. Sending ``fingerprint``
    raises 422 via ``extra='forbid'`` for the same reason
    :class:`TargetCreate` rejects it. ``preferred_impl_id`` is patchable.

    ``version`` is patchable as of G0.15-T6 (#1215) — same fix-class as
    G0.14-T4 #1145's PATCH-on-``product``. An operator who probes the
    target manually (or has out-of-band knowledge of the product
    version) can set it to flip the resolver from the wildcard fallback
    to the versioned connector. Clearing it (``{"version": null}``) is
    legal and returns the target to the wildcard-fallback shape — the
    column is nullable so this is not a constraint violation.
    """

    model_config = ConfigDict(extra="forbid")

    aliases: list[str] | None = None
    product: str | None = Field(default=None, min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=100)
    host: str | None = Field(default=None, max_length=512)
    port: int | None = Field(default=None, ge=1, le=65535)
    fqdn: str | None = None
    secret_ref: str | None = None
    auth_model: AuthModel | None = None
    vpn_required: bool | None = None
    extras: dict[str, Any] | None = None
    notes: str | None = None
    preferred_impl_id: str | None = Field(default=None, max_length=200)
