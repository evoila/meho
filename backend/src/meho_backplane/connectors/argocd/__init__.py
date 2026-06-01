# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.argocd — ArgoCdConnector package.

Importing this package registers :class:`ArgoCdConnector` against the v2
connector registry under the natural key
``(product="argocd", version="3.x", impl_id="argocd-api")`` **and** the
``(product="argocd", version="", impl_id="")`` wildcard fallback — dual
registration from day one per G0.15-T6 (#1215).

Registration is **synchronous (import time)** only at this Task's stage:
the v2 registry entries land via
:func:`~meho_backplane.connectors.registry.register_connector_v2` so the
lookup tables are populated before the lifespan begins and a probe firing
during startup sees a fully-populated registry. The
:func:`~meho_backplane.connectors.registry._eager_import_connectors` walk
discovers this subpackage by directory name, so no manual import-list edit
is needed elsewhere.

The asynchronous (lifespan startup) typed-op registrar — queued via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
the way Harbor (#621) and bind9 (#367) do — lands in this module as of
G3.12-T2 (#1391). :func:`register_argocd_typed_operations` delegates to
:meth:`ArgoCdConnector.register_operations`, which walks
:data:`~meho_backplane.connectors.argocd.ops.ARGOCD_OPS` and upserts the six
curated read-core descriptors (``argocd.app.list`` / ``argocd.app.get`` /
``argocd.app.diff`` / ``argocd.app.resource_tree`` /
``argocd.appproject.list`` / ``argocd.repo.list``) — all ``safety_level="safe"``,
``requires_approval=False``, read-only. The write ops (``app.sync`` /
``rollback`` / ``set``) remain a deferred, approval-gated follow-up.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is intentionally **not** called: ArgoCD has no v1 chassis history,
and the v1 entry would land as ``("argocd", "", "")`` and confuse the
resolver tie-break ladder. Only the v2 triple (+ wildcard) advertises this
class — the same decision Harbor, bind9, NSX, and SDDC Manager made.
"""

from meho_backplane.connectors.argocd.connector import ArgoCdConnector
from meho_backplane.connectors.argocd.ops import (
    ARGOCD_OPS,
    ARGOCD_WHEN_TO_USE_BY_GROUP,
    ArgoCdOp,
)
from meho_backplane.connectors.argocd.session import (
    ARGOCD_TOKEN_FIELD,
    ArgoCdCredentialsLoader,
    ArgoCdTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_argocd_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``ArgoCdConnector.register_operations``.

    The canonical typed-op registration pattern (G0.6-T-Refactor-Vault #390)
    is a module-level ``async def register_xxx_typed_operations`` queued onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    via :func:`register_typed_op_registrar`. argocd implements the underlying
    op walk as a classmethod on :class:`ArgoCdConnector` (so the test suite
    can exercise it without lifespan plumbing); this wrapper is the seam that
    lets the standard registrar mechanism drive it.

    The ``embedding_service`` keyword-only parameter mirrors the bind9 / K8s
    sibling contract: :func:`run_typed_op_registrars` passes the process-wide
    :class:`EmbeddingService` (or a chassis-test stub) to every registrar via
    ``registrar(embedding_service=...)``, so each registrar **must** accept
    the kwarg or the lifespan crashes with :class:`TypeError`. The wrapper
    accepts-and-discards it because
    :meth:`ArgoCdConnector.register_operations` resolves the embedding service
    via ``register_typed_operation``'s process-wide singleton fallback.
    """
    del embedding_service  # see docstring -- kwarg accepted for runner-compatibility
    await ArgoCdConnector.register_operations()


# v2 entry -- the canonical resolver key. The versioned triple always wins
# the resolver tie-break when both it and the wildcard are present.
register_connector_v2(
    product="argocd",
    version="3.x",
    impl_id="argocd-api",
    cls=ArgoCdConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- a target with ``version=None``
# (fresh, unfingerprinted, no operator-asserted version yet) resolves to
# this connector through the resolver's ``versioned_over_wildcard`` step
# rather than 501-ing with ``no_connector``.
register_connector_v2(
    product="argocd",
    version="",
    impl_id="",
    cls=ArgoCdConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The
# runner (``run_typed_op_registrars``) iterates after ``_eager_import_connectors``
# so the descriptor rows land before the first dispatch.
register_typed_op_registrar(register_argocd_typed_operations)

__all__ = [
    "ARGOCD_OPS",
    "ARGOCD_TOKEN_FIELD",
    "ARGOCD_WHEN_TO_USE_BY_GROUP",
    "ArgoCdConnector",
    "ArgoCdCredentialsLoader",
    "ArgoCdOp",
    "ArgoCdTargetLike",
    "load_credentials_from_vault",
    "register_argocd_typed_operations",
]
