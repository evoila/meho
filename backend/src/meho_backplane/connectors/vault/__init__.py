# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.vault — VaultConnector package.

Importing the package registers :class:`VaultConnector` against the v2
connector registry under the natural key ``(product="vault",
version="1.x", impl_id="vault")``, and queues the connector's typed-op
upserts onto the lifespan-driven registrar list so
``endpoint_descriptor`` rows land before the first dispatch.

Registration is split between two phases:

* **Synchronous (import time)** — the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2`
  inside this module. The registry lookup tables are populated before
  the lifespan begins so a probe firing during startup sees a fully-
  populated registry.

* **Asynchronous (lifespan startup)** —
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`,
  which upserts the ``endpoint_descriptor`` rows for every Vault typed
  op. Async because the helper writes to the DB and the embedding-text
  encode step is async. Idempotent: a second pod restart against
  unchanged descriptions is a no-op for the embedding pipeline (see the
  "skip-re-embed" branch in
  :func:`~meho_backplane.operations.typed_register.register_typed_operation`).

The pre-G0.6-T-Refactor v1 :func:`register_connector` entry point is
deliberately **not** called here. v1 dual-writes to both the v1 and v2
registries with ``(product, "", "")`` for the v2 key; since this
connector now advertises an explicit ``(version="1.x", impl_id="vault")``
key, the v1 entry would be a stale duplicate that confuses
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-
break ladder (two unbounded-version candidates with no operator
preference). Removing the v1 entry also drops this connector from
:func:`~meho_backplane.connectors.get_connector` (the v1-keyed lookup);
production callers that need it have already migrated to
:func:`~meho_backplane.connectors.resolver.resolve_connector`. The
shipped :func:`~meho_backplane.api.v1.health._probe_vault_federation`
falls back to direct :class:`VaultConnector` instantiation when the
v1 lookup returns ``None``, so the route keeps working through the
refactor.
"""

from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.vault.connector import VaultConnector, VaultTarget
from meho_backplane.connectors.vault.ops import (
    register_vault_typed_operations,
    vault_kv_read,
)
from meho_backplane.connectors.vault.ops_sys import (
    register_vault_sys_typed_operations,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_connector_v2(
    product="vault",
    version="1.x",
    impl_id="vault",
    cls=VaultConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
# The registrar list is module-scope on
# meho_backplane.operations.typed_register; the lifespan calls
# ``run_typed_op_registrars`` after _eager_import_connectors so every
# connector subpackage has self-registered by the time the runner
# iterates.
register_typed_op_registrar(register_vault_typed_operations)
# The ``sys`` read op group (G3.3-T2 #546) ships its own registrar so
# the KV-v2 (#545) and sys surfaces register independently — no shared
# handler state, distinct endpoint_descriptor rows.
register_typed_op_registrar(register_vault_sys_typed_operations)

__all__ = [
    "VaultConnector",
    "VaultTarget",
    "register_vault_sys_typed_operations",
    "register_vault_typed_operations",
    "vault_kv_read",
]
