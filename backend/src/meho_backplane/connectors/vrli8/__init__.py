# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vrli8 — Vrli8Connector package (legacy vRLI 8.x).

Importing this package registers :class:`Vrli8Connector` against the v2 connector
registry as the **legacy dual-impl** for **vRealize Log Insight 8.x / VMware Aria
Operations for Logs 8.x** — the log-management tier of the VCF-5.x-era source
estate evoila brings customers off (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3068).
vRLI 8.x is the direct predecessor of **VCF Operations for Logs 9.x**
(:mod:`meho_backplane.connectors.vcf_logs`, ``vrli-rest``), so this is a **second
implementation of ``product="vrli"``** — the 5th real two-impl case after
``fleet``, ``vcfa``, ``sddc``, and ``vrops``.

The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors` which walks
every ``connectors/<subpackage>/`` at startup, so both this legacy package and
the modern ``vcf_logs`` package register under ``product="vrli"`` (distinct
``impl_id``s) before any dispatch can occur.

**Only the versioned triple is registered — NOT the ``("vrli","","")`` wildcard**,
which the modern ``vcf_logs`` package owns (a second class on it would raise
``RuntimeError`` at boot). This is the *inversion* of the ``fleet`` case: here the
modern impl owns the wildcard, so an *unfingerprinted* / unversioned ``vrli``
target resolves to modern ``vrli-rest``; a fingerprinted 8.x target
(``/api/v2/version`` reports a real five-part ``8.x`` version) resolves to **this**
impl by its disjoint band (legacy ``>=8.0,<9.0`` vs modern ``>=9.0,<10.0``) — see
:mod:`tests.test_connectors_vrli8_dual_impl_resolution`.

The ``product`` token is ``"vrli"`` and ``impl_id`` is ``"vrli-vrli8"`` because
:func:`register_connector_v2` enforces that ``product`` equals the first
hyphen-segment of ``impl_id`` — the round-trip invariant. ``connector_id``
``"vrli-vrli8-8.0"`` parses back to ``("vrli", "8.0", "vrli-vrli8")``.

The package queues a typed-op registrar (:mod:`.typed_ops`) that upserts the
modern sibling's ``vrli.event.query`` typed op under this impl's triple, so a
vRLI 8.x target dispatches its log-event query on a fresh boot with zero catalog
ingest. Because 8.x publishes no committable API spec (only a per-appliance
runtime Swagger UI — OA3-only ingest #2090 can't consume it), the broader browse
surface the modern sibling serves via ingested catalog is a documented follow-up,
not shipped here.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vrli8.connector import Vrli8Connector
from meho_backplane.connectors.vrli8.typed_ops import register_vrli8_typed_operations
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the vRLI 8.x connector — the
#: dispatch-canonical ``(product, version, impl_id)`` triple
#: :func:`parse_connector_id` derives from ``"vrli-vrli8-8.0"``, plus the derived
#: ``connector_id`` slug. ``product`` is shared with the modern ``vrli-rest``
#: (``vrli``); the ``impl_id`` disambiguates the two impls.
VRLI8_PRODUCT: Final[str] = "vrli"
VRLI8_VERSION: Final[str] = "8.0"
VRLI8_IMPL_ID: Final[str] = "vrli-vrli8"
VRLI8_CONNECTOR_ID: Final[str] = f"{VRLI8_IMPL_ID}-{VRLI8_VERSION}"

register_connector_v2(
    product=VRLI8_PRODUCT,
    version=VRLI8_VERSION,
    impl_id=VRLI8_IMPL_ID,
    cls=Vrli8Connector,
)

# NO wildcard registration. The ``("vrli","","")`` key is owned by the modern
# ``vcf_logs`` package; a second class on it would raise RuntimeError at boot (the
# ``fleet_lcm`` no-wildcard shape, inverted — there the legacy owns the wildcard,
# here the modern does). An unfingerprinted ``vrli`` target therefore resolves to
# modern ``vrli-rest``; a fingerprinted 8.x target resolves here.

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# (``run_typed_op_registrars``) iterates after ``_eager_import_connectors`` so the
# typed descriptor row lands before the first dispatch — the vRLI 8.x read op
# dispatches on a fresh boot with zero catalog ingest (#3068).
register_typed_op_registrar(register_vrli8_typed_operations)

__all__ = [
    "VRLI8_CONNECTOR_ID",
    "VRLI8_IMPL_ID",
    "VRLI8_PRODUCT",
    "VRLI8_VERSION",
    "Vrli8Connector",
    "register_vrli8_typed_operations",
]
