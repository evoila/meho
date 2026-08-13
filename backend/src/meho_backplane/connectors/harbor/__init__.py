# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.harbor — HarborConnector package.

Importing this package registers :class:`HarborConnector` against the
v2 connector registry under
``(product="harbor", version="2.x", impl_id="harbor-rest")``, and
queues the typed-op upserts (the 9-op read core + the two robot
lifecycle writes) onto the lifespan-driven registrar list so
``endpoint_descriptor`` rows land before the first dispatch.

Registration is split between two phases (mirroring the Vault precedent
in :mod:`meho_backplane.connectors.vault`):

* **Synchronous (import time)** — the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2`.

* **Asynchronous (lifespan startup)** —
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes
  :func:`~meho_backplane.connectors.harbor.typed_ops.register_harbor_typed_operations`
  (the audited read core) and
  :func:`~meho_backplane.connectors.harbor.ops.register_harbor_robot_operations`
  (the robot create/delete writes), upserting the ``endpoint_descriptor``
  rows for each.

Both surfaces are **typed** (``source_kind="typed"``): they dispatch on
a fresh boot with zero catalog ingest. The read core was converted from
the retired ingested-curation apparatus (``harbor.core_ops``) under
#2856, mirroring the NSX (#2302) and SDDC Manager (#2306) conversions —
the same Task #2358 fix that closed Goal #2247's "per-deploy
catalog-state failure class."

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="2.x", impl_id="harbor-rest")`` key; the v1 entry would land as
``("harbor", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder. Same pattern :mod:`meho_backplane.connectors.sddc_manager` and
:mod:`meho_backplane.connectors.nsx` established.
"""

from typing import Final

from meho_backplane.connectors.harbor.connector import HarborConnector
from meho_backplane.connectors.harbor.ops import register_harbor_robot_operations
from meho_backplane.connectors.harbor.session import (
    HarborCredentialsLoader,
    HarborTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)
from meho_backplane.connectors.harbor.typed_ops import (
    HARBOR_TYPED_OPS,
    HARBOR_TYPED_WHEN_TO_USE_BY_GROUP,
    HarborTypedOp,
    register_harbor_typed_operations,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

#: Endpoint-descriptor product key — what
#: :func:`~meho_backplane.operations._lookup.parse_connector_id` extracts
#: from ``"harbor-rest-2.x"`` (first hyphen-segment of impl_id
#: ``"harbor-rest"``). Matches :attr:`HarborConnector.product` directly.
HARBOR_PRODUCT: Final[str] = "harbor"
HARBOR_VERSION: Final[str] = "2.x"
HARBOR_IMPL_ID: Final[str] = "harbor-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id``
#: round-trips back to the triple above: ``"harbor-rest-2.x"``.
HARBOR_CONNECTOR_ID: Final[str] = f"{HARBOR_IMPL_ID}-{HARBOR_VERSION}"

register_connector_v2(
    product="harbor",
    version="2.x",
    impl_id="harbor-rest",
    cls=HarborConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="harbor",
    version="",
    impl_id="",
    cls=HarborConnector,
)

# Queue the typed-op upserts onto the lifespan-driven registrar list.
# The read core (harbor.about … harbor.robot.list) and the two robot
# writes (harbor.robot.create is credential_mint — the broadcast
# collapses to aggregate-only so the minted secret never appears in the
# SSE feed) all register as source_kind="typed".
register_typed_op_registrar(register_harbor_typed_operations)
register_typed_op_registrar(register_harbor_robot_operations)

__all__ = [
    "HARBOR_CONNECTOR_ID",
    "HARBOR_IMPL_ID",
    "HARBOR_PRODUCT",
    "HARBOR_TYPED_OPS",
    "HARBOR_TYPED_WHEN_TO_USE_BY_GROUP",
    "HARBOR_VERSION",
    "HarborConnector",
    "HarborCredentialsLoader",
    "HarborTargetLike",
    "HarborTypedOp",
    "SessionCredentials",
    "load_credentials_from_vault",
    "register_harbor_robot_operations",
    "register_harbor_typed_operations",
]
