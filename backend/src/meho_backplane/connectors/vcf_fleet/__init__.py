# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vcf_fleet — VcfFleetConnector package.

Importing this package registers :class:`VcfFleetConnector` against the
v2 connector registry under
``(product="fleet", version="9.0", impl_id="fleet-rest")``. The
chassis lifespan calls
:func:`~meho_backplane.connectors.registry._eager_import_connectors`
which walks every ``connectors/<product>/`` subpackage at startup, so
the registration lands before any dispatch can occur.

The v1 :func:`~meho_backplane.connectors.registry.register_connector`
entry point is deliberately **not** called. The connector advertises an
explicit ``(version="9.0", impl_id="fleet-rest")`` key; the v1 entry
would land as ``("fleet", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s
tie-break ladder. Same pattern :mod:`meho_backplane.connectors.harbor`
and :mod:`meho_backplane.connectors.vcf_automation` established.

Operations for this connector arrive via G0.7 spec ingestion against
the Fleet (vRSLCM-derived) OpenAPI surface. G3.6-T7 (#831) shipped the
connector skeleton; G3.6-T8 (#835) adds the curated read-only v0.5
core — 8 operator-enabled ops + 6 reviewed groups — via the
:func:`~meho_backplane.connectors.vcf_fleet.core_ops.apply_fleet_core_curation`
substrate call against an already-ingested connector.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vcf_fleet.connector import VcfFleetConnector
from meho_backplane.connectors.vcf_fleet.core_ops import (
    FLEET_CONNECTOR_ID,
    FLEET_CORE_GROUPS,
    FLEET_CORE_OPS,
    FLEET_IMPL_ID,
    FLEET_PATH_RULES,
    FLEET_PRODUCT,
    FLEET_VERSION,
    FleetCoreGroup,
    FleetCoreOp,
    apply_fleet_core_curation,
    classify_fleet_op,
)
from meho_backplane.connectors.vcf_fleet.session import (
    SessionCredentials,
    VcfFleetCredentialsLoader,
    VcfFleetTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.vcf_fleet.typed_ops import (
    FLEET_TYPED_OPS,
    FLEET_TYPED_WHEN_TO_USE_BY_GROUP,
    FleetTypedOp,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_fleet_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``VcfFleetConnector.register_operations``.

    The canonical typed-op registration pattern is a module-level
    ``async def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    via :func:`register_typed_op_registrar`. Fleet implements the op walk
    as a classmethod on :class:`VcfFleetConnector` (so the test suite can
    exercise it without lifespan plumbing); this wrapper is the seam that
    lets the standard registrar mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the argocd /
    bind9 sibling contract: :func:`run_typed_op_registrars` passes the
    process-wide :class:`EmbeddingService` (or a chassis-test stub) to
    every registrar, so each registrar **must** accept the kwarg or the
    lifespan crashes with :class:`TypeError`. The wrapper
    accepts-and-discards it because
    :meth:`VcfFleetConnector.register_operations` resolves the embedding
    service via ``register_typed_operation``'s process-wide singleton
    fallback.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await VcfFleetConnector.register_operations()


register_connector_v2(
    product="fleet",
    version="9.0",
    impl_id="fleet-rest",
    cls=VcfFleetConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="fleet",
    version="",
    impl_id="",
    cls=VcfFleetConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The
# runner (``run_typed_op_registrars``) iterates after
# ``_eager_import_connectors`` so the typed descriptor rows land before the
# first dispatch — the audited read set dispatches on a fresh boot with
# zero catalog ingest (T4 · #2304).
register_typed_op_registrar(register_fleet_typed_operations)

__all__ = [
    "FLEET_CONNECTOR_ID",
    "FLEET_CORE_GROUPS",
    "FLEET_CORE_OPS",
    "FLEET_IMPL_ID",
    "FLEET_PATH_RULES",
    "FLEET_PRODUCT",
    "FLEET_TYPED_OPS",
    "FLEET_TYPED_WHEN_TO_USE_BY_GROUP",
    "FLEET_VERSION",
    "FleetCoreGroup",
    "FleetCoreOp",
    "FleetTypedOp",
    "SessionCredentials",
    "VcfFleetConnector",
    "VcfFleetCredentialsLoader",
    "VcfFleetTargetLike",
    "apply_fleet_core_curation",
    "classify_fleet_op",
    "load_credentials_from_vault",
    "register_fleet_typed_operations",
]
