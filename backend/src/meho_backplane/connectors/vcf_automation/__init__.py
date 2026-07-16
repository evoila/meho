# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_automation -- VcfAutomationConnector package.

Importing this package registers :class:`VcfAutomationConnector` against
the v2 connector registry under
``(product="vcfa", version="9.0", impl_id="vcfa-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises an
explicit ``(version="9.0", impl_id="vcfa-rest")`` key; the v1 entry
would land as ``("vcfa", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.nsx` /
:mod:`meho_backplane.connectors.sddc_manager` established.

Operations for this connector arrive via G0.7 dual-plane spec ingestion
-- both the provider plane (``vcf-automation-9.0/cloudapi.yaml``) and
the tenant plane (``vcf-automation-9.0/iaas.yaml``) are ingested under
this single connector with ``spec_source`` tags distinguishing them,
same shape as vSphere's ``vcenter.yaml`` + ``vi-json.yaml``. The audited
read set ships as first-class **typed** ops (:mod:`.typed_ops`,
``source_kind="typed"``, T5 · #2305), dispatchable on a fresh boot with
zero catalog ingest; the wider ingested catalog stays browsable as
profiled-dispatch breadth, enable-able through the generic review flow
(``ReviewService.enable_reads``). The skeleton was shipped at G3.6-T10
(#832); the hand-curated ingested-enable apparatus was retired in #2358
(T7 of #2266).
"""

from typing import Final

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_automation.connector import (
    VcfAutomationConfigurationError,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation.session import (
    SessionCredentials,
    VcfAutomationCredentialsLoader,
    VcfAutomationTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.vcf_automation.typed_ops import (
    VCFA_TYPED_OPS,
    VCFA_TYPED_WHEN_TO_USE_BY_GROUP,
    VcfaTypedOp,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService

#: Endpoint-descriptor identity for the VCFA connector — the dispatch-canonical
#: ``(product, version, impl_id)`` triple :func:`parse_connector_id` derives
#: from ``"vcfa-rest-9.0"``, plus the derived ``connector_id`` slug.
#: :class:`VcfAutomationConnector` pins the same triple as class attributes.
#: Relocated from the retired ``_core_data`` / ``core_ops`` curation modules
#: (#2358) so acceptance / typed-read tests that seed ``EndpointDescriptor``
#: rows import one source of truth.
VCFA_PRODUCT: Final[str] = "vcfa"
VCFA_VERSION: Final[str] = "9.0"
VCFA_IMPL_ID: Final[str] = "vcfa-rest"
VCFA_CONNECTOR_ID: Final[str] = f"{VCFA_IMPL_ID}-{VCFA_VERSION}"


async def register_vcfa_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``VcfAutomationConnector.register_typed_operations``.

    The canonical typed-op registration pattern is a module-level
    ``async def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    via :func:`register_typed_op_registrar`. The VCFA op walk is a
    classmethod on the connector (so the test suite can drive it without
    lifespan plumbing); this wrapper is the seam the standard registrar
    mechanism calls. The ``embedding_service`` kwarg is accepted-and-
    discarded — the runner passes it to every registrar, and
    :meth:`VcfAutomationConnector.register_typed_operations` resolves the
    process-wide singleton via ``register_typed_operation``'s fallback.
    """
    del embedding_service  # runner-compatibility kwarg; singleton resolved downstream
    await VcfAutomationConnector.register_typed_operations()


register_connector_v2(
    product="vcfa",
    version="9.0",
    impl_id="vcfa-rest",
    cls=VcfAutomationConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="vcfa",
    version="",
    impl_id="",
    cls=VcfAutomationConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The
# runner (``run_typed_op_registrars``) iterates after
# ``_eager_import_connectors`` so the five typed read descriptors land
# before the first dispatch — no ingested catalog state required (VCFA
# ships no vendor spec; typed conversion is the only working read path).
register_typed_op_registrar(register_vcfa_typed_operations)

__all__ = [
    "VCFA_CONNECTOR_ID",
    "VCFA_IMPL_ID",
    "VCFA_PRODUCT",
    "VCFA_TYPED_OPS",
    "VCFA_TYPED_WHEN_TO_USE_BY_GROUP",
    "VCFA_VERSION",
    "SessionCredentials",
    "VcfAutomationConfigurationError",
    "VcfAutomationConnector",
    "VcfAutomationCredentialsLoader",
    "VcfAutomationTargetLike",
    "VcfaTypedOp",
    "load_credentials_from_vault",
    "register_vcfa_typed_operations",
]
