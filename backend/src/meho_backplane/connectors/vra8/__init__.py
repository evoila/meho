# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vra8 — Vra8Connector package.

Importing this package registers :class:`Vra8Connector` against the v2 connector
registry as the **legacy dual-impl** for **vRealize Automation 8.x** (vRA 8) — the
migration-source estate evoila brings customers off (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3058). vRA 8
is the direct predecessor of **VCF Automation 9.x**
(:mod:`meho_backplane.connectors.vcf_automation`, ``vcfa-rest``) — the same product
line renamed across a major-version rebuild — so this is a **second implementation of
``product="vcfa"``**, not a net-new product: the second real two-impl case after
``fleet`` (policy #3033 / #3038).

The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors` which walks
every ``connectors/<subpackage>/`` at startup, so both this legacy package and the
modern ``vcf_automation`` package register under ``product="vcfa"`` (distinct
``impl_id``s) before any dispatch can occur — the ``vcf_fleet`` + ``fleet_lcm`` shape.

**Only the versioned triple is registered — NOT the ``("vcfa","","")`` wildcard.**
The wildcard is already owned by the **modern** ``vcf_automation`` package (a second
class on that key would raise ``RuntimeError`` at boot). This is the *inversion* of
the ``fleet`` case (there the *legacy* ``vcf_fleet`` owns the wildcard and the modern
``fleet_lcm`` skips it): here the modern impl owns it, so an *unfingerprinted* /
unversioned ``vcfa`` target resolves to modern ``vcfa-rest`` — a vRA 8 target must
carry an 8.x product version **operator-asserted at onboarding** to resolve to this
legacy impl (the fingerprint reports the API version, not a resolvable product
version). The bands are **disjoint** (legacy ``>=8.0,<9.0`` vs
modern ``>=9.0,<10.0``), so a versioned 8.x target resolves here and a 9.x target to
modern, with no specificity tie — see
:mod:`tests.test_connectors_vra8_dual_impl_resolution`.

The ``product`` token is ``"vcfa"`` and ``impl_id`` is ``"vcfa-vra8"`` because
``register_connector_v2`` enforces that ``product`` equals the first hyphen-segment
of ``impl_id`` — the round-trip invariant. ``connector_id`` ``"vcfa-vra8-8.0"``
parses back to ``("vcfa", "8.0", "vcfa-vra8")``.

This package registers a curated **6-op typed read core** (#3058, :mod:`.typed_ops`)
via :func:`register_typed_op_registrar`, so a vRA 8 target dispatches a
migration-inventory read on a fresh boot with zero catalog ingest. Because vRA 8's
vendor spec is Swagger 2.0 (OA3-only ingest can't consume it — typed, not ingested),
a wider ingested breadth surface is not a follow-up here — the read core is the
connector's surface.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vra8.connector import Vra8Connector
from meho_backplane.connectors.vra8.session import (
    Vra8CredentialsLoader,
    Vra8TargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.vra8.typed_ops import (
    VRA8_TYPED_OPS,
    VRA8_TYPED_WHEN_TO_USE_BY_GROUP,
    Vra8TypedOp,
    register_vra8_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the vRA 8 connector — the dispatch-canonical
#: ``(product, version, impl_id)`` triple :func:`parse_connector_id` derives from
#: ``"vcfa-vra8-8.0"``, plus the derived ``connector_id`` slug. The typed read core
#: rows land under :data:`VRA8_CONNECTOR_ID`. ``product`` is shared with the modern
#: ``vcf-automation`` (``vcfa``); the ``impl_id`` disambiguates the two impls.
VRA8_PRODUCT: Final[str] = "vcfa"
VRA8_VERSION: Final[str] = "8.0"
VRA8_IMPL_ID: Final[str] = "vcfa-vra8"
VRA8_CONNECTOR_ID: Final[str] = f"{VRA8_IMPL_ID}-{VRA8_VERSION}"


register_connector_v2(
    product=VRA8_PRODUCT,
    version=VRA8_VERSION,
    impl_id=VRA8_IMPL_ID,
    cls=Vra8Connector,
)

# NO wildcard registration. The ``("vcfa","","")`` key is owned by the modern
# ``vcf_automation`` package; a second class on it would raise RuntimeError at boot
# (the ``fleet_lcm`` no-wildcard shape, inverted — there the legacy owns the
# wildcard, here the modern does). An unfingerprinted ``vcfa`` target therefore
# resolves to modern ``vcfa-rest``; an 8.x-versioned target resolves here.

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# (``run_typed_op_registrars``) iterates after ``_eager_import_connectors`` so the
# typed descriptor rows land before the first dispatch — the vRA 8 read core
# dispatches on a fresh boot with zero catalog ingest (#3058).
register_typed_op_registrar(register_vra8_typed_operations)

__all__ = [
    "VRA8_CONNECTOR_ID",
    "VRA8_IMPL_ID",
    "VRA8_PRODUCT",
    "VRA8_TYPED_OPS",
    "VRA8_TYPED_WHEN_TO_USE_BY_GROUP",
    "VRA8_VERSION",
    "Vra8Connector",
    "Vra8CredentialsLoader",
    "Vra8TargetLike",
    "Vra8TypedOp",
    "load_credentials_from_vault",
    "register_vra8_typed_operations",
]
