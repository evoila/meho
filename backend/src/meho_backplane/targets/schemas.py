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

import ssl
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.targets.ssrf_guard import assert_public_destination

if TYPE_CHECKING:
    from meho_backplane.db.models import Target as TargetORM

__all__ = [
    "AuthModel",
    "Target",
    "TargetCreate",
    "TargetSummary",
    "TargetUpdate",
    "project_target_to_summary",
    "validate_ca_pin_pem",
]


def validate_ca_pin_pem(value: str | None) -> str | None:
    """Reject a ``tls_ca_pin`` that the stdlib SSL layer can't parse.

    The pin is fed verbatim to
    :meth:`ssl.SSLContext.load_verify_locations` ``(cadata=...)`` at
    dispatch time (#1784). Validating it at the API boundary turns an
    otherwise-runtime ``ssl.SSLError`` (raised inside the never-raises
    connector path, where it would surface as an opaque dispatch failure)
    into a 422 the operator sees the moment they POST/PATCH the target --
    the typed-validation win that motivated a first-class column over an
    unvalidated ``extras`` key.

    ``None`` (no pin) and an all-whitespace string (treated as "clear the
    pin", normalised to ``None``) pass through. A non-empty value must be
    loadable as one or more PEM-encoded certificates;
    :meth:`load_verify_locations` raises :exc:`ssl.SSLError` on malformed
    PEM and requires at least one certificate, so a stray header or a
    truncated block is rejected here rather than at dispatch.
    """
    if value is None:
        return None
    if not value.strip():
        # An empty / all-whitespace pin is "no pin" -- normalise to NULL so
        # the column never stores a meaningless blank string the connector
        # would then try (and fail) to load as a certificate.
        return None
    ctx = ssl.create_default_context()
    try:
        ctx.load_verify_locations(cadata=value)
    except ssl.SSLError as exc:
        raise ValueError(f"tls_ca_pin is not a valid PEM certificate bundle: {exc}") from exc
    return value


class TargetSummary(BaseModel):
    """Short shape for list endpoints.

    G0.16-T6 Finding D (#1312) widened this from the previous narrow
    projection (``id, name, aliases, product, host``) to mirror the
    detail-endpoint shape's identification + connection-routing
    fields, including ``version``, ``port``, ``fqdn``, ``secret_ref``,
    ``auth_model``, ``vpn_required``, ``preferred_impl_id``, and the
    server-managed timestamps. Per
    ``docs/codebase/api-shape-conventions.md`` §5, list endpoints
    must not silently mask fields the detail endpoint exposes
    (RDC #771 Finding 8 caught list returning ``version=null,
    secret_ref=null, preferred_impl_id=null`` for targets whose
    detail endpoint returned actual values; adopters either wrote
    N+1 calls or accepted silent data masking).

    The two remaining omissions vs :class:`Target` are deliberate:
    ``notes`` and ``extras``. Both are operator-authored free-form
    blobs that can carry meaningful payload (``extras`` is
    capability-marker metadata; ``notes`` is operator commentary)
    and shipping them in the list response would inflate the page
    size for the common "give me the names and routing" question
    that list consumers ask. The convention doc's escape valve
    applies: when an N+1 cost on these specifically becomes a real
    concern, a future ``GET /api/v1/targets/summary`` projection
    endpoint can carry the narrow shape under an explicit name
    (anti-pattern is silent masking, not documented projection).

    Frozen so callers can stash instances in request state or
    structured logs without fear of mutation.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    name: str
    aliases: tuple[str, ...]
    product: str
    version: str | None = None
    host: str
    port: int | None
    fqdn: str | None
    secret_ref: str | None
    auth_model: AuthModel
    vpn_required: bool
    # T1 (#1780). Per-target TLS-verification flag. Connection-routing
    # (the dispatch client honours it in #1781), so it MUST be listable
    # in the summary per the api-shape-conventions §5 no-silent-masking
    # rule the class docstring cites. Defaults to ``True`` so legacy
    # hand-constructed instances stay valid; production projections go
    # through :func:`project_target_to_summary` which passes it through.
    verify_tls: bool = True
    # T5 (#1784). Per-target CA-trust pin (PEM). Connection-routing, like
    # ``verify_tls``, so it is listable per the same no-silent-masking
    # rule (§5) -- the list response must not hide a connection-affecting
    # field the detail endpoint exposes. ``None`` = no pin. Defaults to
    # ``None`` for legacy hand-constructed instances; production
    # projections pass it through :func:`project_target_to_summary`.
    tls_ca_pin: str | None = None
    # T (#2002). Per-target TLS SNI / cert-verification hostname,
    # decoupled from ``host``. Connection-routing like ``verify_tls`` /
    # ``tls_ca_pin``, so it is listable per the same §5 no-silent-masking
    # rule -- the list response must not hide a connection-affecting field
    # the detail endpoint exposes. ``None`` = derive from ``host`` as
    # today. Defaults to ``None`` for legacy hand-constructed instances;
    # production projections pass it through
    # :func:`project_target_to_summary`.
    tls_server_name: str | None = None
    fingerprint: Mapping[str, Any] | None
    preferred_impl_id: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


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
    # T1 (#1780). Whether connector dispatch verifies the target's TLS
    # certificate chain. Default-secure (``True``); ``False`` is the
    # audited per-target opt-out for self-signed / internal-CA
    # appliances. Defaults to ``True`` so call sites that have not yet
    # been updated to populate the field (test helpers, historical
    # fixtures, legacy code constructing :class:`Target` by hand) keep
    # working; production read paths go through :func:`_to_full` which
    # passes the column through explicitly. The dispatch path that
    # consumes the flag lands in #1781.
    verify_tls: bool = True
    # T5 (#1784). Per-target CA-trust pin (PEM). ``None`` = no pin (verify
    # against the global bundle only). When set, dispatch trusts this CA
    # while keeping ``CERT_REQUIRED`` + hostname verification on -- the
    # secure supersession of ``verify_tls=false``. Defaults to ``None`` so
    # legacy hand-constructed instances stay valid; production read paths
    # go through :func:`_to_full`.
    tls_ca_pin: str | None = None
    # T (#2002). Per-target TLS SNI / cert-verification hostname,
    # decoupled from ``host`` (the TCP connect address + wire ``Host:``
    # header). ``None`` = derive the SNI / verify name from ``host`` as
    # today. When set, dispatch keeps ``base_url=https://<host>`` (connect
    # + ``Host`` = IP) and threads ``extensions={"sni_hostname": <name>}``
    # so the cert is verified against the override name -- letting an
    # operator keep ``verify_tls=true`` while sending ``Host: <IP>``.
    # Orthogonal to ``verify_tls`` / ``tls_ca_pin`` (no mutual exclusion).
    # Defaults to ``None`` so legacy hand-constructed instances stay
    # valid; production read paths go through :func:`_to_full`.
    tls_server_name: str | None = None
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
    # T1 (#1780). Per-target TLS-verification opt-out. Default-secure:
    # an omitted ``verify_tls`` lands ``True`` so a fresh target verifies
    # the cert chain against the global ``SSL_CERT_FILE`` bundle (today's
    # behaviour, byte-identical). An operator targeting a self-signed /
    # internal-CA appliance passes ``false`` explicitly -- which the
    # route handler audits (durable ``audit_log`` row + WARN log). The
    # dispatch path that consumes the flag lands in #1781.
    verify_tls: bool = True
    # T5 (#1784). Per-target CA-trust pin (PEM). When set, dispatch trusts
    # this CA while keeping ``CERT_REQUIRED`` + hostname verification on
    # (the secure govc-thumbprint path), so it is the secure supersession
    # of ``verify_tls=false`` and **mutually exclusive** with it (see the
    # model validator below). ``None`` (the default) = no pin. The PEM is
    # validated at this boundary (see :func:`validate_ca_pin_pem`) so a
    # malformed bundle is a 422 here, not an opaque dispatch failure.
    tls_ca_pin: str | None = None
    # T (#2002). Per-target TLS SNI / cert-verification hostname,
    # decoupled from ``host``. Default ``None`` = derive the SNI / verify
    # name from ``host`` (today's behaviour, byte-identical). An operator
    # whose appliance pins its cert to an FQDN-CN but only accepts
    # ``Host: <IP>`` sets this to the cert-CN FQDN so dispatch can keep
    # ``verify_tls=true`` while routing the IP. ``max_length`` matches
    # ``host`` (a hostname/FQDN, not a URL). No mutual-exclusion validator
    # -- it composes cleanly with ``verify_tls`` / ``tls_ca_pin``.
    tls_server_name: str | None = Field(default=None, max_length=512)
    extras: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    preferred_impl_id: str | None = Field(default=None, max_length=200)

    @field_validator("tls_ca_pin")
    @classmethod
    def _validate_ca_pin(cls, value: str | None) -> str | None:
        return validate_ca_pin_pem(value)

    @field_validator("host", "fqdn")
    @classmethod
    def _reject_non_public_destination(cls, value: str | None) -> str | None:
        """SSRF guard (evoila-bosnia/meho-internal#153).

        A non-public destination (private / loopback / link-local /
        metadata / CGNAT — any non-globally-routable address) is a
        structured 422 at this boundary unless the operator-configured
        ``MEHO_TARGET_SSRF_ALLOWLIST`` exempts it. The guard screens the
        httpx-normalized *dialed* host, so a value carrying URL
        structure cannot screen as one destination and dial another —
        see :mod:`meho_backplane.targets.ssrf_guard` for the full
        contract (fail-open on unresolvable hostnames; the connect path
        re-checks the *resolved* address before every dispatch).
        """
        if value is not None:
            assert_public_destination(value)
        return value

    @model_validator(mode="after")
    def _ca_pin_excludes_insecure(self) -> TargetCreate:
        """Reject a target that both pins a CA *and* disables verification.

        The two are contradictory: a CA-pin is the *secure* way to reach a
        self-signed appliance (verification stays on, against the pinned
        CA), so combining it with ``verify_tls=false`` (verification off)
        is an operator error -- the insecure flag would silently win at
        dispatch and throw away the pin's protection. Fail closed at the
        API boundary so the contradiction never reaches the connector.
        """
        if self.tls_ca_pin is not None and not self.verify_tls:
            raise ValueError(
                "tls_ca_pin and verify_tls=false are mutually exclusive: a "
                "CA-pin already reaches a self-signed / internal-CA endpoint "
                "securely (verification stays on against the pinned CA), so "
                "disabling verification as well is contradictory. Drop one."
            )
        return self


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
    # T1 (#1780). Patchable per-target TLS-verification flag. ``None`` is
    # the absent-marker ("client did not send this field", the standard
    # PATCH-field semantics every field here uses), so a PATCH that does
    # not touch ``verify_tls`` binds **no** TLS audit keys. An explicit
    # ``{"verify_tls": false}`` flips the column and is audited by the
    # route handler (durable ``audit_log`` row + WARN log); ``true``
    # re-enables verification. Unlike the nullable string columns,
    # ``verify_tls`` is ``NOT NULL`` at the DB layer, so there is no
    # "clear to null" state -- the value is always one of ``True`` /
    # ``False`` once present.
    verify_tls: bool | None = None
    # T5 (#1784). Patchable per-target CA-trust pin (PEM). Standard
    # PATCH-field semantics: ``None`` is the absent-marker ("client did
    # not send this field"), so a PATCH that does not mention
    # ``tls_ca_pin`` leaves the column untouched. Unlike ``verify_tls``
    # the column is nullable, so an explicit ``{"tls_ca_pin": null}``
    # clears the pin (same as clearing ``secret_ref`` / ``fqdn``). A
    # non-null value is PEM-validated here, and combining it with
    # ``verify_tls=false`` in the *same* body is rejected (the merged
    # state across the persisted row is enforced in the route handler).
    tls_ca_pin: str | None = None
    # T (#2002). Patchable per-target TLS SNI / cert-verification
    # hostname. Standard PATCH-field semantics: ``None`` is the
    # absent-marker ("client did not send this field"), so a PATCH that
    # does not mention ``tls_server_name`` leaves the column untouched;
    # an explicit ``{"tls_server_name": null}`` clears the override (same
    # as clearing ``secret_ref`` / ``fqdn`` / ``tls_ca_pin``) and returns
    # the target to deriving the SNI / verify name from ``host``. No
    # mutual-exclusion validator -- orthogonal to ``verify_tls`` /
    # ``tls_ca_pin``.
    tls_server_name: str | None = Field(default=None, max_length=512)
    extras: dict[str, Any] | None = None
    notes: str | None = None
    preferred_impl_id: str | None = Field(default=None, max_length=200)

    @field_validator("tls_ca_pin")
    @classmethod
    def _validate_ca_pin(cls, value: str | None) -> str | None:
        return validate_ca_pin_pem(value)

    @field_validator("host", "fqdn")
    @classmethod
    def _reject_non_public_destination(cls, value: str | None) -> str | None:
        """SSRF guard (evoila-bosnia/meho-internal#153) — same as create.

        ``None`` is the PATCH absent-marker and passes untouched; a
        non-null ``host``/``fqdn`` is screened exactly like
        :class:`TargetCreate` so update cannot be used to re-point an
        existing target into private space.
        """
        if value is not None:
            assert_public_destination(value)
        return value

    @model_validator(mode="after")
    def _ca_pin_excludes_insecure(self) -> TargetUpdate:
        """Reject a PATCH body that pins a CA *and* disables verification.

        Same contradiction as on :class:`TargetCreate`, but scoped to the
        body: only fires when **both** ``tls_ca_pin`` (non-null) and
        ``verify_tls=false`` are sent in the one request. The cross-row
        merged check (e.g. PATCH sets a pin on a row already at
        ``verify_tls=false``) is the route handler's job -- the schema
        only sees the request body.
        """
        if self.tls_ca_pin is not None and self.verify_tls is False:
            raise ValueError(
                "tls_ca_pin and verify_tls=false are mutually exclusive: a "
                "CA-pin already reaches a self-signed / internal-CA endpoint "
                "securely, so disabling verification as well is contradictory. "
                "Send only one."
            )
        return self


def project_target_to_summary(t: TargetORM) -> TargetSummary:
    """Project a :class:`TargetORM` row to the wire :class:`TargetSummary` shape.

    G0.16-T6 review-iter-1 m1 (#1312). Single canonical projection
    for both the ``GET /api/v1/targets`` list endpoint
    (:mod:`meho_backplane.api.v1.targets`) and the
    :func:`~meho_backplane.targets.resolver.resolve_target`
    near-miss / ambiguity diagnostics
    (:mod:`meho_backplane.targets.resolver`). The two sites
    previously held byte-for-byte duplicate ``_to_summary`` helpers;
    the drift class they invited is exactly what Finding D caught
    (list silently masking ``version`` / ``secret_ref`` /
    ``preferred_impl_id`` while detail returned them). One helper,
    one place to change, no drift.

    Coerces ``aliases`` from the ORM column's mutable ``list[str]``
    JSON shape to the wire schema's ``tuple[str, ...]`` so the
    frozen :class:`TargetSummary` instance is genuinely immutable,
    and wraps the raw ``auth_model`` string in the
    :class:`~meho_backplane.connectors.schemas.AuthModel` enum so
    callers get the typed value the schema declares.
    """
    return TargetSummary(
        id=t.id,
        tenant_id=t.tenant_id,
        name=t.name,
        # ORM stores aliases as ``list[str]`` (mutable JSON column);
        # the response schema declares ``tuple[str, ...]`` for
        # frozen-model immutability. Coerce at the boundary.
        aliases=tuple(t.aliases),
        product=t.product,
        version=t.version,
        host=t.host,
        port=t.port,
        fqdn=t.fqdn,
        secret_ref=t.secret_ref,
        auth_model=AuthModel(t.auth_model),
        vpn_required=t.vpn_required,
        verify_tls=t.verify_tls,
        tls_ca_pin=t.tls_ca_pin,
        tls_server_name=t.tls_server_name,
        fingerprint=t.fingerprint,
        preferred_impl_id=t.preferred_impl_id,
        created_at=t.created_at,
        updated_at=t.updated_at,
        deleted_at=t.deleted_at,
    )
