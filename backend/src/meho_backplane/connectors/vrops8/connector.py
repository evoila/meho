# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vrops8Connector — legacy dual-impl for vRealize Operations 8.x (#3067).

vRealize Operations 8.x / VMware Aria Operations 8.x (the 8.10 rebrand is
name-only, no API break — ``major`` stays ``8`` across the whole line) is the
direct predecessor of **VCF Operations 9.x**
(:mod:`meho_backplane.connectors.vcf_operations`, ``vrops-rest``). It is the
monitoring tier of a VCF-5.x-era source estate evoila migrates customers *off*
(initiative `evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_,
task #3067). This is a **second implementation of ``product="vrops"``**, not a
net-new product — the 4th real two-impl case after ``fleet``, ``vcfa``, and
``sddc``.

Why a subclass (not a standalone copy)
--------------------------------------

Unlike the sibling legacy connectors #3057 (vCD — net-new), #3058 (vRA 8 —
divergent two-step CSP→IaaS auth), and #3059 (SDDC 5.x — divergent read scope),
the vROps 8.x Suite API surface is **identical** to 9.x: the same
``POST /suite-api/api/auth/token/acquire`` session mint, the same
``GET /suite-api/api/versions/current`` fingerprint, and the same
``/suite-api/api/{alerts, resources/query, resources/stats/latest}`` read paths
(all stable since vROps 6.6/7.0). So the honest, minimal representation
(CLAUDE.md postulates 2 + 3) is a **thin subclass** of
:class:`~meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector`
that inherits the acquire flow, the auth-model gate, the per-target session
cache, the ``invalidate_session`` / ``invalidate_credentials`` (#2067) recovery,
the fingerprint, and all four typed read handlers unchanged — overriding only
the registration triple, the version band, and the token *scheme* (below).

The dispatcher rebinds an inherited handler against the resolver-chosen
instance (:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`
walks ``Vrops8Connector.__mro__`` and matches the base-class function object —
the documented bind9-E2E subclass pattern), so the inherited handlers run on a
:class:`Vrops8Connector` instance with the 8.x scheme (they call
``self.auth_headers``) and the 8.x band.

The one behavioural delta — the token scheme
--------------------------------------------

The 9.x connector presents ``Authorization: OpsToken <token>`` — the 9.x-native
scheme (#2395). An 8.x appliance predates the ``OpsToken`` alias and accepts
only the token-era ``vRealizeOpsToken`` scheme (stable across the whole 8.x
line). :meth:`auth_headers` therefore re-labels the scheme on the header the
base class returns; the auth-model guard and the token mint stay single-sourced
on the base (the security-sensitive path is not duplicated).

Resolution — by fingerprint, with a parseability caveat
-------------------------------------------------------

**Only the versioned triple ``(vrops, 8.0, vrops-vrops8)`` is registered — NOT
the ``("vrops","","")`` wildcard.** The wildcard is owned by the modern
``vcf_operations`` package (a second class on that key would raise
``RuntimeError`` at boot). This is the *inversion* of the ``fleet`` case: the
modern impl owns the wildcard, so an *unfingerprinted* / unversioned ``vrops``
target resolves to modern ``vrops-rest``. Unlike vRA 8 (whose appliance
fingerprint reports the API version rather than a product version), a vROps 8.x
appliance's ``versions/current`` returns a real product ``releaseName`` (a dotted
release, observed shape e.g. ``8.18.0.24178391``), so a fingerprinted 8.x target
normally resolves to **this** impl by its disjoint band
(legacy ``>=8.0,<9.0`` vs modern ``>=9.0,<10.0``) — no re-band, no specificity
tie, no operator-asserted version required.

**Caveat (the #3067 review's top finding, a named residual risk):** the
fingerprint is inherited and stores ``releaseName`` *raw* (no split/normalise).
Resolution therefore depends on ``releaseName`` being PEP 440-parseable. If a
given 8.x appliance were to report a non-parseable ``releaseName`` (a word or a
space), the resolver cannot band it and falls back to the modern-owned wildcard —
i.e. it resolves *open* to ``vrops-rest``, which presents the 9.x ``OpsToken``
scheme and would 401 against the 8.x appliance. The observed ``releaseName`` is
always a dotted numeric release (and the modern connector relies on the same
field), so this is expected not to occur — but it is **unverified against a live
8.x appliance** (the deferred live-verify tail). The escape hatch is
operator-side: pin ``version`` or ``preferred_impl_id`` on the target. The
fallback behaviour is pinned in
:mod:`tests.test_connectors_vrops8_dual_impl_resolution`.

The ``product`` token is ``"vrops"`` and ``impl_id`` is ``"vrops-vrops8"``
because :func:`register_connector_v2` enforces that ``product`` equals the first
hyphen-segment of ``impl_id`` (the round-trip invariant); ``connector_id``
``"vrops-vrops8-8.0"`` parses back to ``("vrops", "8.0", "vrops-vrops8")``.
"""

from __future__ import annotations

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vcf_operations.connector import VcfOperationsConnector
from meho_backplane.connectors.vcf_operations.session import VcfOperationsTargetLike

__all__ = ["Vrops8Connector"]

#: The pre-9.x authorization scheme for an acquired suite-api token. vROps /
#: Aria Operations 8.x appliances predate the ``OpsToken`` alias (#2395) the
#: modern connector presents and accept only ``vRealizeOpsToken`` (the token-era
#: scheme, stable across the whole 8.x line). This is the sole behavioural delta
#: from the 9.x sibling.
_VROPS8_TOKEN_SCHEME = "vRealizeOpsToken"


class Vrops8Connector(VcfOperationsConnector):
    """vRealize Operations 8.x legacy read connector — a thin subclass of ``vrops-rest``.

    Overrides the registration triple, the version band, and the token scheme;
    inherits everything else from
    :class:`~meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector`
    (see the module docstring for why a subclass is the honest shape here). The
    inherited ``__init__`` gives this class its own per-target session-token
    cache, lock, and credentials cache (keyed under ``product_label="vrops"``),
    independent of the modern instance the dispatcher caches separately.
    """

    # G0.6 v2 registry metadata — the dispatch-canonical triple
    # ``parse_connector_id`` derives from ``"vrops-vrops8-8.0"``. ``product`` is
    # shared with the modern ``vrops-rest``; the ``impl_id`` disambiguates.
    product = "vrops"
    version = "8.0"
    impl_id = "vrops-vrops8"
    supported_version_range = ">=8.0,<9.0"
    priority = 1

    async def auth_headers(
        self,
        target: VcfOperationsTargetLike,
        operator: Operator,
    ) -> dict[str, str]:
        """Return ``{"Authorization": "vRealizeOpsToken <token>"}`` for the request.

        Identical to the inherited 9.x path — same
        :func:`~meho_backplane.connectors._shared.vcf_auth.is_acceptable_auth_model`
        gate, same ``POST /suite-api/api/auth/token/acquire`` session mint, same
        per-target token cache — except the presented scheme is the pre-9.x
        ``vRealizeOpsToken`` (an 8.x appliance predates the ``OpsToken`` alias,
        #2395). The guard and the mint stay single-sourced on the base class;
        this override only re-labels the scheme on the header the base returns,
        so a future change to the base auth path (e.g. the gate) is inherited
        rather than silently forked.
        """
        headers = dict(await super().auth_headers(target, operator))
        _scheme, _sep, token = headers.get("Authorization", "").partition(" ")
        headers["Authorization"] = f"{_VROPS8_TOKEN_SCHEME} {token}"
        return headers
