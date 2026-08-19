# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcd — VcloudDirectorConnector package.

Importing this package registers :class:`VcloudDirectorConnector` against the v2
connector registry as the **net-new typed** connector for the legacy standalone
**VMware Cloud Director** (vCD) product — the migration-source estate evoila
brings customers off (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3057).
vCD is a distinct product from the Broadcom-era successor: VCF Automation
(:mod:`meho_backplane.connectors.vcf_automation`) absorbed only vCD's provider
control plane, so this is its own ``product="vcd"`` connector, not a ``vcfa``
dual-impl.

The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors` which walks
every ``connectors/<product>/`` subpackage at startup, so this registration
lands before any dispatch can occur (no explicit wiring elsewhere).

**Both a versioned triple and the ``("vcd", "", "")`` wildcard are registered.**
Unlike ``fleet-lcm`` — which skips the wildcard because the legacy ``vcf_fleet``
package already owns ``("fleet", "", "")`` — vCD is a brand-new product with no
sibling, so it takes the wildcard itself (the harbor / nsx / keycloak /
vcf-automation single-impl pattern). The wildcard makes an *unfingerprinted* vCD
target still resolve; the versioned triple carries the real
``(product, version, impl_id)`` coordinates the typed ops register under and the
``connector_id`` (``"vcd-rest-10.6"``) parses to. The ``product`` token is
``"vcd"`` (not ``"vcloud-director"``) because ``register_connector_v2`` enforces
that ``product`` equals the first hyphen-segment of ``impl_id`` — a hyphenated
product token would crash the round-trip check at boot.

This package registers a curated **7-op typed read core** (#3057, :mod:`.typed_ops`)
via :func:`register_typed_op_registrar`, so a vCD target dispatches a
migration-inventory read on a fresh boot with zero catalog ingest. Because vCD
publishes no committable OpenAPI (typed, not ingested), a wider ingested breadth
surface is not a follow-up here — the read core is the connector's surface.
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcd.connector import VcloudDirectorConnector
from meho_backplane.connectors.vcd.session import (
    VcloudDirectorCredentialsLoader,
    VcloudDirectorTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.vcd.typed_ops import (
    VCD_TYPED_OPS,
    VCD_TYPED_WHEN_TO_USE_BY_GROUP,
    VcloudDirectorTypedOp,
    register_vcd_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the vCD connector — the dispatch-canonical
#: ``(product, version, impl_id)`` triple :func:`parse_connector_id` derives from
#: ``"vcd-rest-10.6"``, plus the derived ``connector_id`` slug. The typed read
#: core rows land under :data:`VCD_CONNECTOR_ID`.
VCD_PRODUCT: Final[str] = "vcd"
VCD_VERSION: Final[str] = "10.6"
VCD_IMPL_ID: Final[str] = "vcd-rest"
VCD_CONNECTOR_ID: Final[str] = f"{VCD_IMPL_ID}-{VCD_VERSION}"


register_connector_v2(
    product=VCD_PRODUCT,
    version=VCD_VERSION,
    impl_id=VCD_IMPL_ID,
    cls=VcloudDirectorConnector,
)

# Wildcard fallback so an unfingerprinted vCD target still resolves. vCD is a
# net-new single-impl product with no sibling owning ``("vcd", "", "")`` (the
# harbor / nsx / keycloak / vcf-automation pattern; contrast fleet-lcm, which
# skips it because vcf_fleet owns ``("fleet", "", "")``).
register_connector_v2(
    product=VCD_PRODUCT,
    version="",
    impl_id="",
    cls=VcloudDirectorConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The runner
# (``run_typed_op_registrars``) iterates after ``_eager_import_connectors`` so the
# typed descriptor rows land before the first dispatch — the vCD read core
# dispatches on a fresh boot with zero catalog ingest (#3057).
register_typed_op_registrar(register_vcd_typed_operations)

__all__ = [
    "VCD_CONNECTOR_ID",
    "VCD_IMPL_ID",
    "VCD_PRODUCT",
    "VCD_TYPED_OPS",
    "VCD_TYPED_WHEN_TO_USE_BY_GROUP",
    "VCD_VERSION",
    "VcloudDirectorConnector",
    "VcloudDirectorCredentialsLoader",
    "VcloudDirectorTargetLike",
    "VcloudDirectorTypedOp",
    "load_credentials_from_vault",
    "register_vcd_typed_operations",
]
