# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vrops8 — Vrops8Connector package (legacy vROps 8.x).

Importing this package registers :class:`Vrops8Connector` against the v2
connector registry as the **legacy dual-impl** for **vRealize Operations 8.x /
VMware Aria Operations 8.x** — the monitoring tier of the VCF-5.x-era source
estate evoila brings customers off (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3067).
vROps 8.x is the direct predecessor of **VCF Operations 9.x**
(:mod:`meho_backplane.connectors.vcf_operations`, ``vrops-rest``) — the same
product line across a major-version rebuild — so this is a **second
implementation of ``product="vrops"``**, the 4th real two-impl case after
``fleet``, ``vcfa``, and ``sddc``.

The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors` which walks
every ``connectors/<subpackage>/`` at startup, so both this legacy package and
the modern ``vcf_operations`` package register under ``product="vrops"`` (distinct
``impl_id``s) before any dispatch can occur — the ``vcf_fleet`` + ``fleet_lcm``
shape.

**Only the versioned triple is registered — NOT the ``("vrops","","")`` wildcard.**
The wildcard is already owned by the modern ``vcf_operations`` package (a second
class on that key would raise ``RuntimeError`` at boot). This is the *inversion*
of the ``fleet`` case: here the modern impl owns the wildcard, so an
*unfingerprinted* / unversioned ``vrops`` target resolves to modern ``vrops-rest``;
a fingerprinted 8.x target (``versions/current`` reports a real dotted ``8.x``
``releaseName``) resolves to **this** impl by its disjoint band
(legacy ``>=8.0,<9.0`` vs modern ``>=9.0,<10.0``) — see
:mod:`tests.test_connectors_vrops8_dual_impl_resolution`. Because the inherited
fingerprint stores ``releaseName`` raw, resolution depends on it being PEP
440-parseable; a non-parseable value falls back *open* to the modern wildcard
(operator pins ``version`` / ``preferred_impl_id`` to override) — the named
residual risk in :class:`~meho_backplane.connectors.vrops8.connector.Vrops8Connector`.

The ``product`` token is ``"vrops"`` and ``impl_id`` is ``"vrops-vrops8"`` because
:func:`register_connector_v2` enforces that ``product`` equals the first
hyphen-segment of ``impl_id`` — the round-trip invariant. ``connector_id``
``"vrops-vrops8-8.0"`` parses back to ``("vrops", "8.0", "vrops-vrops8")``.

The package queues a typed-op registrar (:mod:`.typed_ops`) that upserts the
modern sibling's audited 4-op read set under this impl's triple, so a vROps 8.x
target dispatches a migration-inventory read on a fresh boot with zero catalog
ingest. Because 8.x publishes no committable OpenAPI 3.x (only a per-instance
Swagger 2.0 document — OA3-only ingest #2090 can't consume it), a wider ingested
breadth surface is not a follow-up here — the typed read core is the connector's
surface.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vrops8.connector import Vrops8Connector
from meho_backplane.connectors.vrops8.typed_ops import register_vrops8_typed_operations
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the vROps 8.x connector — the
#: dispatch-canonical ``(product, version, impl_id)`` triple
#: :func:`parse_connector_id` derives from ``"vrops-vrops8-8.0"``, plus the
#: derived ``connector_id`` slug (the typed read core rows land under it).
#: ``product`` is shared with the modern ``vrops-rest`` (``vrops``); the
#: ``impl_id`` disambiguates the two impls.
VROPS8_PRODUCT: Final[str] = "vrops"
VROPS8_VERSION: Final[str] = "8.0"
VROPS8_IMPL_ID: Final[str] = "vrops-vrops8"
VROPS8_CONNECTOR_ID: Final[str] = f"{VROPS8_IMPL_ID}-{VROPS8_VERSION}"

register_connector_v2(
    product=VROPS8_PRODUCT,
    version=VROPS8_VERSION,
    impl_id=VROPS8_IMPL_ID,
    cls=Vrops8Connector,
)

# NO wildcard registration. The ``("vrops","","")`` key is owned by the modern
# ``vcf_operations`` package; a second class on it would raise RuntimeError at
# boot (the ``fleet_lcm`` no-wildcard shape, inverted — there the legacy owns the
# wildcard, here the modern does). An unfingerprinted ``vrops`` target therefore
# resolves to modern ``vrops-rest``; a fingerprinted 8.x target resolves here.

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# (``run_typed_op_registrars``) iterates after ``_eager_import_connectors`` so the
# typed descriptor rows land before the first dispatch — the vROps 8.x read core
# dispatches on a fresh boot with zero catalog ingest (#3067).
register_typed_op_registrar(register_vrops8_typed_operations)

__all__ = [
    "VROPS8_CONNECTOR_ID",
    "VROPS8_IMPL_ID",
    "VROPS8_PRODUCT",
    "VROPS8_VERSION",
    "Vrops8Connector",
    "register_vrops8_typed_operations",
]
