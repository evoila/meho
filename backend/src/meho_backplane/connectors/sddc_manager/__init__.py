# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.sddc_manager — SddcManagerConnector package.

Importing this package registers :class:`SddcManagerConnector` against the
v2 connector registry under
``(product="sddc", version="9.0", impl_id="sddc-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so the
registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="9.0", impl_id="sddc-rest")`` key; the v1 entry would land as
``("sddc", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder. Same pattern :mod:`meho_backplane.connectors.nsx` established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the idempotency
check there will no-op on the
``(product="sddc", version="9.0", impl_id="sddc-rest")`` triple
because this module has already registered the hand-rolled class. Until then,
this module is the only registration path.

Operations span two surfaces. The audited 12-read lab-audit set (#2306)
ships as first-class **typed** ops in :mod:`.typed_ops` / :mod:`.typed_reads`
(``source_kind="typed"``), dispatchable on a fresh boot with zero catalog
ingest. The four non-audited reads (release, domain detail, network-pools,
bundles) and the wider VCF API catalog arrive via G0.7 spec ingestion
against the ``endpoint_descriptor`` table and stay browsable as
profiled-dispatch breadth, enable-able through the generic review flow
(``ReviewService.enable_reads``) — the hand-curated ingested-enable
apparatus was retired in #2358 (T7 of #2266).
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.sddc_manager.connector import SddcManagerConnector
from meho_backplane.connectors.sddc_manager.profile import SDDC_EXECUTION_PROFILE
from meho_backplane.connectors.sddc_manager.session import (
    SddcCredentialsLoader,
    SddcTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)
from meho_backplane.connectors.sddc_manager.typed_ops import (
    SDDC_TYPED_OPS,
    SddcTypedOp,
    register_sddc_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor identity for the SDDC Manager connector — the
#: dispatch-canonical ``(product, version, impl_id)`` triple
#: :func:`parse_connector_id` derives from ``"sddc-rest-9.0"``, plus the
#: derived ``connector_id`` slug. :class:`SddcManagerConnector` pins the same
#: triple as class attributes. Relocated from the retired ``core_ops``
#: curation module (#2358) so acceptance / typed-read tests that seed
#: ``EndpointDescriptor`` rows import one source of truth.
SDDC_PRODUCT: Final[str] = "sddc"
SDDC_VERSION: Final[str] = "9.0"
SDDC_IMPL_ID: Final[str] = "sddc-rest"
SDDC_CONNECTOR_ID: Final[str] = f"{SDDC_IMPL_ID}-{SDDC_VERSION}"

register_connector_v2(
    product="sddc",
    version="9.0",
    impl_id="sddc-rest",
    cls=SddcManagerConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="sddc",
    version="",
    impl_id="",
    cls=SddcManagerConnector,
)

# Queue the audited-read typed-op upsert (#2306) onto the lifespan-driven
# registrar list. The runner (``run_typed_op_registrars``) iterates after
# ``_eager_import_connectors`` so the ``source_kind='typed'`` descriptor
# rows land before the first dispatch -- no catalog ingest required.
register_typed_op_registrar(register_sddc_typed_operations)

__all__ = [
    "SDDC_CONNECTOR_ID",
    "SDDC_EXECUTION_PROFILE",
    "SDDC_IMPL_ID",
    "SDDC_PRODUCT",
    "SDDC_TYPED_OPS",
    "SDDC_VERSION",
    "SddcCredentialsLoader",
    "SddcManagerConnector",
    "SddcTargetLike",
    "SddcTypedOp",
    "SessionCredentials",
    "load_credentials_from_vault",
    "register_sddc_typed_operations",
]
