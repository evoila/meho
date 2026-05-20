# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.harbor — HarborConnector package.

Importing this package registers :class:`HarborConnector` against the
v2 connector registry under
``(product="harbor", version="2.x", impl_id="harbor-rest")``, and
queues the robot lifecycle typed-op upserts onto the lifespan-driven
registrar list so ``endpoint_descriptor`` rows land before the first
dispatch.

Registration is split between two phases (mirroring the Vault precedent
in :mod:`meho_backplane.connectors.vault`):

* **Synchronous (import time)** — the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2`.

* **Asynchronous (lifespan startup)** —
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :func:`~meho_backplane.connectors.harbor.ops.register_harbor_robot_operations`,
  which upserts the ``endpoint_descriptor`` rows for ``harbor.robot.create``
  and ``harbor.robot.delete``.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="2.x", impl_id="harbor-rest")`` key; the v1 entry would land as
``("harbor", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder. Same pattern :mod:`meho_backplane.connectors.sddc_manager` and
:mod:`meho_backplane.connectors.nsx` established.

Once G0.7-T8 (#408) lands its
:func:`ensure_connector_class_registered` auto-shim in main, the idempotency
check there will no-op on the
``(product="harbor", version="2.x", impl_id="harbor-rest")`` triple
because this module has already registered the hand-rolled class. Until then,
this module is the only registration path.

Spec-ingested read ops (#620) arrive via G0.7 ingestion of the Harbor 2.x
OpenAPI spec. The robot lifecycle ops (create/delete) ship here in #621 as
hand-registered typed ops — the redaction contract (``credential_mint``
classification) is load-bearing and must not depend on spec-derived
``op_class`` heuristics.
"""

from meho_backplane.connectors.harbor.connector import HarborConnector
from meho_backplane.connectors.harbor.core_ops import (
    HARBOR_CONNECTOR_ID,
    HARBOR_CORE_GROUPS,
    HARBOR_CORE_OPS,
    HARBOR_IMPL_ID,
    HARBOR_PATH_RULES,
    HARBOR_PRODUCT,
    HARBOR_VERSION,
    HarborCoreGroup,
    HarborCoreOp,
    apply_harbor_core_curation,
    classify_harbor_op,
)
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.session import (
    HarborCredentialsLoader,
    HarborTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_connector_v2(
    product="harbor",
    version="2.x",
    impl_id="harbor-rest",
    cls=HarborConnector,
)

# Queue the robot lifecycle typed-op upsert onto the lifespan-driven
# registrar list. harbor.robot.create is classified credential_mint —
# the broadcast collapses to aggregate-only so the minted secret never
# appears in the SSE feed.
register_typed_op_registrar(register_harbor_robot_operations)

__all__ = [
    "HARBOR_CONNECTOR_ID",
    "HARBOR_CORE_GROUPS",
    "HARBOR_CORE_OPS",
    "HARBOR_IMPL_ID",
    "HARBOR_PATH_RULES",
    "HARBOR_PRODUCT",
    "HARBOR_VERSION",
    "HarborConnector",
    "HarborCoreGroup",
    "HarborCoreOp",
    "HarborCredentialsLoader",
    "HarborTargetLike",
    "SessionCredentials",
    "apply_harbor_core_curation",
    "classify_harbor_op",
    "load_credentials_from_vault",
    "register_harbor_robot_operations",
]
