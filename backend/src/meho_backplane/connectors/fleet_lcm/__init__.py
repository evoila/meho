# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.fleet_lcm — FleetLcmConnector package.

Importing this package registers :class:`FleetLcmConnector` against the v2
connector registry under
``(product="fleet", version="9.0", impl_id="fleet-lcm")`` — the **modern**
generic implementation of ``product=fleet``, coexisting with the legacy
typed ``fleet-rest`` impl
(:mod:`meho_backplane.connectors.vcf_fleet`). This is the first real
two-impl case in the codebase; the two are resolved per target by
fingerprint (:func:`~meho_backplane.connectors.resolver.resolve_connector`):
a 9.0 target resolves ``fleet-lcm`` by its narrower ``>=9.0,<10.0`` band
(most-specific-version tie-break), an 8.x target resolves ``fleet-rest``
(the only in-range candidate after #3037's ``>=8.0,<10.0`` re-band). The
decision of record is ``versioned-connector-dual-impl`` (initiative
`evoila/meho#3033 <https://github.com/evoila/meho/issues/3033>`_).

The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors` which
walks every ``connectors/<product>/`` subpackage at startup, so the
registration lands before any dispatch can occur.

**No wildcard sibling registration.** Unlike the typed connectors' G0.15-T6
(#1215) ``(product, "", "")`` fanout, this package registers **only** the
versioned triple. The legacy ``vcf_fleet`` package already registers the
``("fleet", "", "")`` wildcard fallback; a second class on that same
wildcard key would raise :exc:`RuntimeError` at import
(``register_connector_v2`` rejects a duplicate three-tuple key). One
wildcard per product is enough — it exists so an *unfingerprinted* fleet
target still resolves *something*; which concrete class it points at does
not matter for the "resolve at all" guarantee, and the versioned entries
carry the real per-band matching once a version is known.

Operations for this connector arrive via **G0.7 spec ingestion** against
the public Apache-2.0 ``fleet-lcm-openapi.yaml``
(``vmware/vcf-api-specs``) as ``source_kind="ingested"``
``endpoint_descriptor`` rows under ``connector_id="fleet-lcm-9.0"`` — they
are not hand-coded. Registering this hand-rolled :class:`HttpConnector`
subclass is the load-bearing prerequisite for those rows to become
dispatchable (a bare ``GenericRestConnector`` auto-shim is
non-dispatchable), the
:class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`
pattern.
"""

from typing import Final

from meho_backplane.connectors.fleet_lcm.connector import FleetLcmConnector
from meho_backplane.connectors.fleet_lcm.session import (
    FleetLcmCredentialsLoader,
    FleetLcmTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2

#: Endpoint-descriptor identity for the modern Fleet LCM connector — the
#: dispatch-canonical ``(product, version, impl_id)`` triple
#: :func:`parse_connector_id` derives from ``"fleet-lcm-9.0"``, plus the
#: derived ``connector_id`` slug. :class:`FleetLcmConnector` pins the same
#: triple as class attributes; the ingested ``/v1/*`` rows land under
#: :data:`FLEET_LCM_CONNECTOR_ID`.
FLEET_LCM_PRODUCT: Final[str] = "fleet"
FLEET_LCM_VERSION: Final[str] = "9.0"
FLEET_LCM_IMPL_ID: Final[str] = "fleet-lcm"
FLEET_LCM_CONNECTOR_ID: Final[str] = f"{FLEET_LCM_IMPL_ID}-{FLEET_LCM_VERSION}"


register_connector_v2(
    product=FLEET_LCM_PRODUCT,
    version=FLEET_LCM_VERSION,
    impl_id=FLEET_LCM_IMPL_ID,
    cls=FleetLcmConnector,
)

__all__ = [
    "FLEET_LCM_CONNECTOR_ID",
    "FLEET_LCM_IMPL_ID",
    "FLEET_LCM_PRODUCT",
    "FLEET_LCM_VERSION",
    "FleetLcmConnector",
    "FleetLcmCredentialsLoader",
    "FleetLcmTargetLike",
    "load_credentials_from_vault",
]
