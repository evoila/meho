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

from datetime import UTC, datetime
from typing import Any

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.base import ShimKind
from meho_backplane.connectors.profile import ExecutionProfile, split_version
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

_NO_PROFILE = (
    "ProfiledRestConnector has no ExecutionProfile attached; the profile "
    "is stamped onto the synthesised connector class at review time "
    "(record_profile_stamp, #1971). A profiled class with no profile is a "
    "registration fault."
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

    #: The reviewed declarative profile, stamped onto the synthesised
    #: subclass at review time (``record_profile_stamp``, #1971). The base
    #: class carries ``None`` — it is concrete (instantiable) so it can
    #: stand in for the dispatchable tier in resolver / dispatcher
    #: classification tests, but a *registered* profiled class always
    #: carries a concrete :class:`ExecutionProfile`. T6 (#1972) reads
    #: ``fingerprint`` / ``probe`` / ``pagination`` off it; T4 (#1970) will
    #: read ``auth`` off the same attribute.
    profile: ExecutionProfile | None = None

    def _require_profile(self) -> ExecutionProfile:
        """Return the attached profile, or raise a registration-fault error."""
        if self.profile is None:
            raise NotImplementedError(_NO_PROFILE)
        return self.profile

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        """Raise until the profile's named auth scheme is wired (T4 #1970)."""
        raise NotImplementedError(_PROFILE_PENDING)

    async def fingerprint(
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Fingerprint the upstream from the profile's declarative recipe.

        Reads :attr:`ExecutionProfile.fingerprint`: GETs its ``path``,
        reads the version string from the literal top-level ``version_key``,
        and renders it into ``(version, build)`` via the named
        :func:`~meho_backplane.connectors.profile.split_version` splitter
        (harbor's ``-`` split, vRLI's 5-part dot split). On transport or
        status failure, returns a non-reachable result whose
        ``extras["error"]`` carries the exception class + message — the same
        shape the hand-coded harbor / SDDC / NSX connectors established.

        ``operator`` is threaded through to the auth-bearing GET when the
        recipe is ``authenticated``; an unauthenticated fingerprint endpoint
        (vRLI's ``/api/v2/version``) does not need it. When the recipe is
        authenticated and no operator is supplied, the call falls through to
        :meth:`auth_headers` (which raises until T4 wires it) — the same
        operator-context requirement the hand-coded connectors carry.
        """
        spec = self._require_profile().fingerprint
        probed_at = datetime.now(UTC)
        product = self.product
        try:
            if spec.authenticated:
                if operator is None:
                    raise RuntimeError(
                        f"fingerprint recipe for {product!r} is authenticated but "
                        "no operator was supplied"
                    )
                payload = await self._get_json(target, spec.path, operator=operator)
            else:
                payload = await self._get_unauthenticated_json(target, spec.path)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor=product,
                product=product,
                reachable=False,
                probed_at=probed_at,
                probe_method=f"GET {spec.path}",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        raw_version = payload.get(spec.version_key)
        version_str, build_str = split_version(
            spec.version_splitter,
            raw_version if isinstance(raw_version, str) else None,
        )
        return FingerprintResult(
            vendor=product,
            product=product,
            version=version_str,
            build=build_str,
            reachable=True,
            probed_at=probed_at,
            probe_method=f"GET {spec.path}",
        )

    async def probe(self, target: Any) -> ProbeResult:
        """Probe reachability from the profile's declarative recipe.

        When :attr:`ExecutionProfile.probe` is the ``'delegate'`` sentinel,
        the probe runs the fingerprint round-trip and reports ``ok`` =
        ``reachable`` (the SDDC Manager / NSX precedent). When it is a
        :class:`~meho_backplane.connectors.profile.ProbeSpec`, GETs its
        ``path`` and compares the literal top-level ``ok_field`` value
        against ``ok_value`` (harbor's ``GET /api/v2.0/health`` with
        ``status == 'healthy'``).

        A dedicated health probe is run unauthenticated — it is a
        reachability check, not a credentialled read; this matches harbor's
        health endpoint, which needs no auth. On transport / status failure
        the probe returns ``ok=False`` with the exception in ``reason``.
        """
        profile = self._require_profile()
        probed_at = datetime.now(UTC)
        if profile.probe == "delegate":
            fp = await self.fingerprint(target)
            reason = None if fp.reachable else str(fp.extras.get("error") or "unreachable")
            return ProbeResult(ok=fp.reachable, reason=reason, probed_at=probed_at)
        spec = profile.probe
        try:
            payload = await self._get_unauthenticated_json(target, spec.path)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                probed_at=probed_at,
            )
        actual = payload.get(spec.ok_field)
        if actual == spec.ok_value:
            return ProbeResult(ok=True, probed_at=probed_at)
        return ProbeResult(
            ok=False,
            reason=f"{spec.ok_field}={actual!r} (expected {spec.ok_value!r})",
            probed_at=probed_at,
        )

    async def _get_unauthenticated_json(self, target: Any, path: str) -> dict[str, Any]:
        """GET *path* with no auth headers, returning parsed JSON.

        The fingerprint/probe recipes may target an unauthenticated version
        / health endpoint (vRLI's ``/api/v2/version``, harbor's
        ``/api/v2.0/health``). The base :meth:`HttpConnector._get_json`
        always calls :meth:`auth_headers` (which raises on a profiled
        connector until T4 wires it), so this seam issues the request
        through the pooled, TLS-trust-aware client without an auth header.
        """
        client = await self._http_client(target)
        resp = await client.request("GET", path)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

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
