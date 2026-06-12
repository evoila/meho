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
from meho_backplane.connectors.vault.connector import VaultConnector
from meho_backplane.connectors.vault.ops import (
    register_vault_typed_operations,
    vault_kv_delete,
    vault_kv_list,
    vault_kv_put,
    vault_kv_read,
    vault_kv_versions,
)
from meho_backplane.connectors.vault.ops_auth import (
    VaultAuthBackendNotMountedError,
    register_vault_auth_operations,
)
from meho_backplane.connectors.vault.ops_identity_token import (
    register_vault_identity_token_operations,
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

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="vault",
    version="",
    impl_id="",
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
# The identity (entity/group/alias) + token (create/revoke_accessor/
# list_accessors) op groups (G3.15-T4 #1412) ship their own registrar so
# they register independently of the KV / sys / auth surfaces. Writes are
# requires_approval=True; vault.token.create's response token is redacted
# via the credential_mint op-class allowlist.
register_typed_op_registrar(register_vault_identity_token_operations)

__all__ = [
    "VaultAuthBackendNotMountedError",
    "VaultConnector",
    "register_vault_auth_operations",
    "register_vault_identity_token_operations",
    "register_vault_sys_typed_operations",
    "register_vault_typed_operations",
    "vault_kv_delete",
    "vault_kv_list",
    "vault_kv_put",
    "vault_kv_read",
    "vault_kv_versions",
]
