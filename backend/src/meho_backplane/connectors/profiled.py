# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Base for ingested REST connectors made dispatchable by an ExecutionProfile.

G0.28-T1 (#1967) — the **gating** half of Initiative #1965 (make ingested
REST read ops dispatchable from a reviewed declarative profile). The
operation-execution path is already declarative: ``dispatch_ingested`` runs
an ingested op off its stored :class:`~meho_backplane.db.models.EndpointDescriptor`
row with no per-vendor Python. The only hand-coded surface blocking an
ingested REST connector from dispatching is ``auth_headers()`` (plus
``fingerprint`` / ``probe``). The auto-shim
(:class:`~meho_backplane.operations.ingest.connector_registration.GenericRestConnector`)
raises :class:`NotImplementedError` exactly there, so a spec-ingested
connector is non-dispatchable.

:class:`ProfiledRestConnector` is the **sibling** of ``GenericRestConnector``
— a :class:`~meho_backplane.connectors.adapters.http.HttpConnector` subclass,
**not** a ``GenericRestConnector`` subclass — that a vetted ``ExecutionProfile``
plugs into to fill that one slot with reviewed declarative data instead of
hand-written Python. Being a sibling (not a subclass) is load-bearing: the
former ``issubclass(GenericRestConnector)`` dispatchability discriminator
would otherwise silently demote a profiled connector as a dead shim and
strip its profile. G0.28-T1 replaces that binary predicate with the
tri-state :func:`~meho_backplane.connectors.base.shim_kind` classifier; this
class is its ``"profiled"`` tier.

Why ``"profiled"`` is its own tier (not just folded into ``"none"``): a
profiled connector carries a bounded ``supported_version_range`` derived
from the ingested spec's version, which can be *narrower* than a shipped
hand-coded class's broad range. If profiled were classified identically to
a hand-coded class, the resolver's most-specific-version-match step would
let a profiled connector out-specific — and therefore shadow — a bespoke
hand-coded connector for the same ``(product, version)``, reinstating the
#1750/#1798 product-shadowing footgun. The tri-state ladder
(``none`` > ``profiled`` > ``bare``) in
:func:`~meho_backplane.connectors.resolver._demote_lower_dispatch_tiers`
keeps a profiled connector *above* a bare shim (it is dispatchable) but
*below* a hand-coded class (a bespoke connector always wins), with
``priority = 0`` so it never out-ranks on the priority rung either.

Scope of T1 — the class + the tri-state classification only. The
``ExecutionProfile`` schema and the named auth catalog land in T3 (#1969);
the hoisted session-lifecycle / token-cache machinery in T4 (#1970); the
profile-driven ``fingerprint`` / ``probe`` and pagination in T6 (#1972).
Until those land, the four overridden methods below raise
:class:`NotImplementedError` with a profile-oriented message — distinct
from the auto-shim's "replace with a per-product subclass" guidance,
because the remediation here is "attach a reviewed ``ExecutionProfile``",
not "write Python". A ``ProfiledRestConnector`` that reaches dispatch
before its profile machinery is wired is therefore classified
``unsupported_feature`` by the dispatcher, **never** ``unreplaced_auto_shim``
— it is not a dead shim, it is a dispatchable connector whose profile
wiring is incomplete.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.base import ShimKind
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["ProfiledRestConnector"]

_PROFILE_PENDING = (
    "ProfiledRestConnector requires a reviewed ExecutionProfile to "
    "dispatch; the profile schema (G0.28-T3 #1969), session/token "
    "machinery (T4 #1970), and profile-driven fingerprint/probe (T6 "
    "#1972) are not wired yet. Attach a vetted profile rather than "
    "hand-coding this method."
)


class ProfiledRestConnector(HttpConnector):
    """Sibling of ``GenericRestConnector`` for profile-driven ingested REST.

    A concrete :class:`~meho_backplane.connectors.adapters.http.HttpConnector`
    subclass (inheriting its client pooling / retry / TLS-trust transport)
    classified ``"profiled"`` so the tri-state resolver treats it as
    dispatchable — above a bare auto-shim, below a hand-coded class. Carries
    the default ``priority = 0``; registered profiled classes advertise a
    bounded ``supported_version_range`` (derived from the ingested spec's
    version, the same shape the auto-shim derives) so they beat a bare shim
    on dispatchability but never out-specific a bespoke hand-coded class.

    The method bodies are intentionally degenerate in T1 — they raise
    :class:`NotImplementedError` with profile-oriented guidance until the
    profile machinery lands in T3/T4/T6. The class is concrete (instantiable)
    so it can stand in for the dispatchable tier in resolver / dispatcher
    classification tests without a hand-faked stand-in.
    """

    # G0.28-T1 (#1967) — the "profiled" tier of the tri-state classifier.
    _shim_kind: ShimKind = "profiled"

    # Explicit (matches the inherited default) so the resolver-relevant
    # contract is readable at the class: a profiled connector never wins the
    # priority rung against a hand-coded class.
    priority: int = 0

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        """Raise until the profile's named auth scheme is wired (T3/T4)."""
        raise NotImplementedError(_PROFILE_PENDING)

    async def fingerprint(
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Raise until the profile-driven fingerprint is wired (T6)."""
        del operator  # unused until the profile machinery lands
        raise NotImplementedError(_PROFILE_PENDING)

    async def probe(self, target: Any) -> ProbeResult:
        """Raise until the profile-driven probe is wired (T6)."""
        raise NotImplementedError(_PROFILE_PENDING)

    async def execute(
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Raise — ingested ops dispatch through ``dispatch_ingested``, not here.

        Like every typed connector's ``execute``, this is a dead legacy
        shim: an ingested op runs off its ``EndpointDescriptor`` row via the
        dispatcher, not through a per-connector ``execute``. The raise keeps
        a stray direct call loud rather than silently degenerate; the
        :class:`OperationResult` annotation satisfies the
        :class:`~meho_backplane.connectors.base.Connector` ABC.
        """
        raise NotImplementedError(_PROFILE_PENDING)
