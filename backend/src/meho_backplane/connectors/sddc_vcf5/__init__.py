# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.sddc_vcf5 — SddcVcf5Connector package.

Importing this package registers :class:`SddcVcf5Connector` against the v2 connector
registry as the **legacy dual-impl** for **VCF 5.x SDDC Manager** — the
migration-source estate evoila brings customers off (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3059). It is
a **second implementation of ``product="sddc"``** (sibling to the modern 9.x
:mod:`meho_backplane.connectors.sddc_manager`, ``sddc-rest``), fingerprint-resolved
per target — the same ``vcf_fleet`` + ``fleet_lcm`` shape.

The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors` which walks
every ``connectors/<subpackage>/`` at startup, so both this legacy package and the
modern ``sddc_manager`` package register under ``product="sddc"`` (distinct
``impl_id``s) before any dispatch can occur.

**Only the versioned triple is registered — NOT the ``("sddc","","")`` wildcard.**
The wildcard is already owned by the modern ``sddc_manager`` package (a second class
on that key would raise ``RuntimeError`` at boot). This is the *inversion* of the
``fleet`` case: here the modern impl owns the wildcard, so an *unfingerprinted* /
unversioned ``sddc`` target resolves to modern ``sddc-rest``. In practice a VCF 5.x
target fingerprints straight into the ``>=5.0,<9.0`` band (SDDC Manager exposes a
real product version at ``GET /v1/sddc-managers``), so it resolves to this legacy
impl without needing an operator-asserted version. The bands are **disjoint** (legacy
``>=5.0,<9.0`` vs modern ``>=9.0,<10.0``), so no specificity tie — see
:mod:`tests.test_connectors_sddc_vcf5_dual_impl_resolution`.

The ``product`` token is ``"sddc"`` and ``impl_id`` is ``"sddc-vcf5"`` because
``register_connector_v2`` enforces that ``product`` equals the first hyphen-segment
of ``impl_id`` — the round-trip invariant. ``connector_id`` ``"sddc-vcf5-5.0"``
parses back to ``("sddc", "5.0", "sddc-vcf5")``.

This package registers a curated **7-op typed read core** (#3059, :mod:`.typed_ops`)
via :func:`register_typed_op_registrar`, so a VCF 5.x target dispatches a
migration-inventory read on a fresh boot with zero catalog ingest. Because 5.x
publishes only a Swagger 2.0 definition (OA3-only ingest can't consume it — typed,
not ingested), a wider ingested breadth surface is not a follow-up here — the read
core is the connector's surface.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.sddc_manager.session import (
    SddcCredentialsLoader,
    SddcTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.sddc_vcf5.connector import SddcVcf5Connector
from meho_backplane.connectors.sddc_vcf5.typed_ops import (
    SDDC_VCF5_TYPED_OPS,
    SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP,
    SddcVcf5TypedOp,
    register_sddc_vcf5_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the VCF 5.x SDDC Manager connector — the
#: dispatch-canonical ``(product, version, impl_id)`` triple :func:`parse_connector_id`
#: derives from ``"sddc-vcf5-5.0"``, plus the derived ``connector_id`` slug. ``product``
#: is shared with the modern ``sddc-rest`` (``sddc``); the ``impl_id`` disambiguates.
SDDC_VCF5_PRODUCT: Final[str] = "sddc"
SDDC_VCF5_VERSION: Final[str] = "5.0"
SDDC_VCF5_IMPL_ID: Final[str] = "sddc-vcf5"
SDDC_VCF5_CONNECTOR_ID: Final[str] = f"{SDDC_VCF5_IMPL_ID}-{SDDC_VCF5_VERSION}"


register_connector_v2(
    product=SDDC_VCF5_PRODUCT,
    version=SDDC_VCF5_VERSION,
    impl_id=SDDC_VCF5_IMPL_ID,
    cls=SddcVcf5Connector,
)

# NO wildcard registration. The ``("sddc","","")`` key is owned by the modern
# ``sddc_manager`` package; a second class on it would raise RuntimeError at boot (the
# ``fleet_lcm`` no-wildcard shape, inverted — there the legacy owns the wildcard, here
# the modern does). An unfingerprinted ``sddc`` target resolves to modern
# ``sddc-rest``; a fingerprinted 5.x target resolves here.

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# iterates after ``_eager_import_connectors`` so the typed descriptor rows land before
# the first dispatch — the VCF 5.x read core dispatches on a fresh boot with zero
# catalog ingest (#3059).
register_typed_op_registrar(register_sddc_vcf5_typed_operations)

__all__ = [
    "SDDC_VCF5_CONNECTOR_ID",
    "SDDC_VCF5_IMPL_ID",
    "SDDC_VCF5_PRODUCT",
    "SDDC_VCF5_TYPED_OPS",
    "SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP",
    "SDDC_VCF5_VERSION",
    "SddcCredentialsLoader",
    "SddcTargetLike",
    "SddcVcf5Connector",
    "SddcVcf5TypedOp",
    "load_credentials_from_vault",
    "register_sddc_vcf5_typed_operations",
]
