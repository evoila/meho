# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.nsx -- the NSX connector package.

Importing this package registers two implementations of NSX against the
v2 connector registry. The resolver picks one per target by the
target's detected version:

* :class:`NsxConnector` under ``(product="nsx", version="9.0",
  impl_id="nsx-rest")`` -- NSX 9.x (the VCF-9 line), band
  ``>=9.0,<10.0``. It also owns the ``("nsx", "", "")`` wildcard, so a
  target with no detected version resolves here.
* :class:`Nsx4Connector` under ``(product="nsx", version="4.2",
  impl_id="nsx-rest")`` -- the standalone NSX 4.x line, band
  ``>=4.0,<9.0``. A thin subclass of :class:`NsxConnector`; see
  :mod:`meho_backplane.connectors.nsx.connector_4x`.

The chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registrations land before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connectors advertise
explicit ``(version, impl_id="nsx-rest")`` keys; the v1 entry would
land as ``("nsx", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.vmware_rest`
established.

Both implementations register the same audited typed reads
(:data:`NSX_TYPED_OPS`, same op ids) under their own triple, so an NSX
4.x and an NSX 9.x target both dispatch them on a fresh boot with zero
catalog ingest. The wider NSX surface stays as ingested browse breadth.
"""

from typing import Final

from meho_backplane.connectors.nsx.connector import NsxConnector
from meho_backplane.connectors.nsx.connector_4x import Nsx4Connector
from meho_backplane.connectors.nsx.session import (
    NsxSessionLoader,
    NsxTargetLike,
    SessionCredentials,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.nsx.typed_ops import (
    NSX_TYPED_OPS,
    NsxTypedOp,
    register_nsx4_typed_operations,
    register_nsx_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the NSX connector — the dispatch-canonical
#: ``(product, version, impl_id)`` triple :func:`parse_connector_id` derives
#: from ``"nsx-rest-9.0"``, plus the derived ``connector_id`` slug (the class
#: pin; finer VCF-9 9.x labels ingest under their own ``nsx-rest-<label>``
#: slug). :class:`NsxConnector` pins the same triple as class attributes.
#: Relocated from the retired ``core_ops`` curation module (#2358) so
#: acceptance / typed-read / VCF-9-ingest tests that seed ``EndpointDescriptor``
#: rows import one source of truth.
NSX_PRODUCT: Final[str] = "nsx"
NSX_VERSION: Final[str] = "9.0"
NSX_IMPL_ID: Final[str] = "nsx-rest"
NSX_CONNECTOR_ID: Final[str] = f"{NSX_IMPL_ID}-{NSX_VERSION}"

#: The NSX 4.x implementation's version pin and connector id
#: (``"nsx-rest-4.2"``). It shares ``product`` and ``impl_id`` with the
#: 9.x implementation; :class:`Nsx4Connector` pins the same triple.
NSX4_VERSION: Final[str] = "4.2"
NSX4_CONNECTOR_ID: Final[str] = f"{NSX_IMPL_ID}-{NSX4_VERSION}"

register_connector_v2(
    product="nsx",
    version="9.0",
    impl_id="nsx-rest",
    cls=NsxConnector,
)

# The NSX 4.x implementation: the versioned triple only. The
# ``("nsx", "", "")`` wildcard below stays owned by the 9.x class, so a
# 4.x target resolves here by its disjoint band and an unversioned
# target resolves to the 9.x class. Same shape as ``vmware-rest-8.0``.
register_connector_v2(
    product="nsx",
    version=NSX4_VERSION,
    impl_id="nsx-rest",
    cls=Nsx4Connector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="nsx",
    version="",
    impl_id="",
    cls=NsxConnector,
)

# Queue the audited-read typed-op upsert (#2302) onto the lifespan-driven
# registrar list. The runner (``run_typed_op_registrars``) iterates after
# ``_eager_import_connectors`` so the ``source_kind='typed'`` descriptor
# rows land before the first dispatch -- no catalog ingest required.
register_typed_op_registrar(register_nsx_typed_operations)
register_typed_op_registrar(register_nsx4_typed_operations)

__all__ = [
    "NSX4_CONNECTOR_ID",
    "NSX4_VERSION",
    "NSX_CONNECTOR_ID",
    "NSX_IMPL_ID",
    "NSX_PRODUCT",
    "NSX_TYPED_OPS",
    "NSX_VERSION",
    "Nsx4Connector",
    "NsxConnector",
    "NsxSessionLoader",
    "NsxTargetLike",
    "NsxTypedOp",
    "SessionCredentials",
    "load_session_credentials_from_vault",
    "register_nsx4_typed_operations",
    "register_nsx_typed_operations",
]
