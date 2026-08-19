# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vrli8Connector — legacy dual-impl for vRealize Log Insight 8.x (#3068).

vRealize Log Insight 8.x / VMware Aria Operations for Logs 8.x (the 8.12 rebrand
is name-only, no API break) is the direct predecessor of **VCF Operations for
Logs 9.x** (:mod:`meho_backplane.connectors.vcf_logs`, ``vrli-rest``). It is the
log-management tier of a VCF-5.x-era source estate evoila migrates customers
*off* (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3068).
This is a **second implementation of ``product="vrli"``**, not a net-new product
— the 5th real two-impl case after ``fleet``, ``vcfa``, ``sddc``, and ``vrops``.

Why a subclass (not a standalone copy)
--------------------------------------

The vRLI ``/api/v2/*`` REST surface is **identical** on 8.x and 9.x — the v2 API
shipped in vRLI 8.0 and is stable through 8.18.x: the same
``POST /api/v2/sessions`` → ``Authorization: Bearer <sessionId>`` auth, the same
unauthenticated ``GET /api/v2/version`` fingerprint, the same
``/api/v2/events/{constraints}`` query, and the same ``{401, 440}``
session-expiry recovery (the appliance's ``trait.authenticated.440`` idle
timeout). So the honest, minimal representation (CLAUDE.md postulates 2 + 3) is
a **thin subclass** of
:class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector` that
inherits the session mint, the token cache, the ``invalidate_session`` /
``invalidate_credentials`` (#2067) recovery, the profile-derived fingerprint,
and the ``vrli.event.query`` typed handler unchanged — overriding **only** the
registration triple and the version band. Unlike vROps 8.x, the ``Bearer``
scheme is unchanged across the version boundary, so there is **no auth override**
at all.

The dispatcher rebinds the inherited ``event_query`` handler against the
resolver-chosen instance
(:func:`~meho_backplane.operations._handler_resolve.is_unbound_method` walks
``Vrli8Connector.__mro__`` — the documented bind9-E2E subclass pattern), so it
runs on a :class:`Vrli8Connector` instance with the 8.x band.

Resolution — by fingerprint alone
----------------------------------

**Only the versioned triple ``(vrli, 8.0, vrli-vrli8)`` is registered — NOT the
``("vrli","","")`` wildcard**, which the modern ``vcf_logs`` package owns (a
second class on it would raise ``RuntimeError`` at boot — the *fleet inversion*).
An *unfingerprinted* / unversioned ``vrli`` target therefore resolves to modern
``vrli-rest``; a fingerprinted 8.x target (``/api/v2/version`` reports a real
five-part ``8.x`` version string) resolves to **this** impl by its disjoint band
(legacy ``>=8.0,<9.0`` vs modern ``>=9.0,<10.0``) — no re-band, no specificity
tie. See :mod:`tests.test_connectors_vrli8_dual_impl_resolution`.

The ``product`` token is ``"vrli"`` and ``impl_id`` is ``"vrli-vrli8"`` because
:func:`register_connector_v2` enforces that ``product`` equals the first
hyphen-segment of ``impl_id`` — the round-trip invariant. ``connector_id``
``"vrli-vrli8-8.0"`` parses back to ``("vrli", "8.0", "vrli-vrli8")``.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_logs.connector import VcfLogsConnector

__all__ = ["Vrli8Connector"]


class Vrli8Connector(VcfLogsConnector):
    """vRealize Log Insight 8.x legacy read connector — a thin subclass of ``vrli-rest``.

    Overrides only the registration triple and the version band; inherits
    everything else from
    :class:`~meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector`
    (see the module docstring — the vRLI v2 surface, incl. the ``Bearer`` auth
    scheme, is identical on 8.x and 9.x). The inherited ``__init__`` gives this
    class its own per-target session cache, lock, and credentials cache (keyed
    under ``product_label="vrli"``), independent of the modern instance the
    dispatcher caches separately.
    """

    # G0.6 v2 registry metadata — the dispatch-canonical triple
    # ``parse_connector_id`` derives from ``"vrli-vrli8-8.0"``. ``product`` is
    # shared with the modern ``vrli-rest``; the ``impl_id`` disambiguates.
    product = "vrli"
    version = "8.0"
    impl_id = "vrli-vrli8"
    supported_version_range = ">=8.0,<9.0"
    priority = 1
