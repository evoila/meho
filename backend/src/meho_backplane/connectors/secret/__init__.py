# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.secret — the secret broker (Initiative #581).

The first **synthetic** connector subpackage: no vendor connector backs
it. Importing the package (the lifespan's
:func:`~meho_backplane.connectors.registry._eager_import_connectors` pass
walks ``connectors/<product>/`` and imports each subpackage) wires the
secret broker in two import-time steps:

* **Adapter registration** — importing
  :mod:`~meho_backplane.connectors.secret.vault_endpoint` runs its
  module-level :func:`~meho_backplane.connectors.secret.endpoints.register_secret_endpoint`
  call, populating :data:`~meho_backplane.connectors.secret.endpoints.SECRET_ENDPOINT_REGISTRY`
  with the vault-kv pair under kind ``"vault"``. Sibling tasks add
  further kinds (#1578 keycloak sink) by importing their adapter module
  the same way.
* **Op registrar queueing** — the
  :func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`
  call below appends
  :func:`~meho_backplane.connectors.secret.ops.register_secret_broker_operations`
  to the lifespan-driven registrar list, so the ``secret.move``
  ``endpoint_descriptor`` row lands before the first dispatch.

Unlike every other connector subpackage, this one calls neither
``register_connector`` nor ``register_connector_v2``: the synthetic
``secret-broker-1.x`` identity has no connector class. The ``secret.move``
handler is a module-level function the dispatcher routes to with
``connector_instance=None`` / ``target=None``.
"""

# Importing the adapter module runs its module-level
# ``register_secret_endpoint("vault", ...)`` call so the registry is
# populated before any move dispatches. Imported for the side effect.
from meho_backplane.connectors.secret import vault_endpoint as _vault_endpoint  # noqa: F401
from meho_backplane.connectors.secret.ops import register_secret_broker_operations
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the secret.move typed-op upsert onto the lifespan-driven
# registrar list (run after the connector eager-import pass).
register_typed_op_registrar(register_secret_broker_operations)

__all__ = [
    "register_secret_broker_operations",
]
