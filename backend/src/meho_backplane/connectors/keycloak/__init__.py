# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.keycloak -- KeycloakConnector package.

G3.13-T1 (#1393) substrate. Importing the package registers
:class:`KeycloakConnector` against the v2 connector registry under the
natural key ``(product="keycloak", version="26.x", impl_id="keycloak-admin")``
**and** the ``(product, "", "")`` wildcard, and queues the connector's
typed-op registrar onto the lifespan-driven registrar list.

Two-phase registration (same shape as ``connectors/bind9/__init__.py``):

* **Synchronous (import time)** -- the v2 registry entries land via
  :func:`~meho_backplane.connectors.registry.register_connector_v2` in
  this module, so the registry lookup tables are populated before the
  lifespan begins and a probe firing during startup sees a fully
  populated registry.

* **Asynchronous (lifespan startup)** --
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`register_keycloak_typed_operations`, which delegates to
  :meth:`KeycloakConnector.register_operations`. In T1 that method ships
  **zero** ops -- the seam is wired now so T2 (read ops) only fills the
  op walk; the registrar plumbing doesn't move.

Like bind9, the keycloak connector does **not** call the v1
``register_connector`` -- it has no chassis-route history. Both the
versioned triple and the wildcard land via ``register_connector_v2``
directly, keeping the resolver tie-break ladder unambiguous.
"""

from meho_backplane.connectors.keycloak.connector import KeycloakConnector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar
from meho_backplane.retrieval.embedding import EmbeddingService


async def register_keycloak_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Module-level registrar wrapper for ``KeycloakConnector.register_operations``.

    The canonical typed-op registration pattern queues a module-level
    ``async def register_xxx_typed_operations`` onto
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    via :func:`register_typed_op_registrar`. The ``embedding_service``
    keyword-only parameter mirrors the bind9 sibling's contract -- the
    runner passes the process-wide :class:`EmbeddingService` (or a
    chassis-test stub) to every registrar via
    ``registrar(embedding_service=...)``, so each registrar **must**
    accept the kwarg or the lifespan crashes with :class:`TypeError`. T1
    ships zero ops so the value is unused; the kwarg-accept-and-discard
    shape matches the bind9 sibling.
    """
    del embedding_service  # T1 ships zero ops -- kwarg accepted for runner-compatibility
    await KeycloakConnector.register_operations()


__all__ = [
    "KeycloakConnector",
    "register_keycloak_typed_operations",
]


# v2 entry -- the canonical resolver key. keycloak has no v1 chassis
# history, so the v1 ``register_connector`` write is intentionally
# omitted (see module docstring).
register_connector_v2(
    product="keycloak",
    version="26.x",
    impl_id="keycloak-admin",
    cls=KeycloakConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- a target with ``version=None``
# (fresh, unfingerprinted, no operator-asserted version yet) resolves to
# this connector through the resolver's ``versioned_over_wildcard`` step
# rather than 501-ing with ``no_connector``. The versioned entry above
# always wins when both are present (resolver tie-break step 1).
register_connector_v2(
    product="keycloak",
    version="",
    impl_id="",
    cls=KeycloakConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list. The
# lifespan calls ``run_typed_op_registrars`` after _eager_import_connectors
# so every connector subpackage has self-registered by the time the
# runner iterates.
register_typed_op_registrar(register_keycloak_typed_operations)
