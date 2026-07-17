# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.gcloud — GcloudConnector package.

Importing this package registers :class:`GcloudConnector` against the
v2 connector registry under
``(product="gcloud", version="1.0", impl_id="gcloud-rest")``.

Two-phase registration (same shape as
:mod:`meho_backplane.connectors.bind9.__init__` and
:mod:`meho_backplane.connectors.kubernetes.__init__`):

* **Synchronous (import time)** — the v2 registry entry lands via
  :func:`~meho_backplane.connectors.registry.register_connector_v2`
  inside this module.

* **Asynchronous (lifespan startup)** —
  :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
  invokes :meth:`GcloudConnector.register_gcloud_typed_operations`, which
  delegates to :func:`~meho_backplane.operations.typed_register.register_typed_operation`
  for each of the eight G3.7-T5 (#848) read-only ops.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="1.0", impl_id="gcloud-rest")`` key; the v1 entry would land as
``("gcloud", "", "")`` and confuse the resolver's tie-break ladder. Same
pattern :mod:`meho_backplane.connectors.harbor` established.

Decision #12 (transport = B: HttpConnector + google-auth impersonation)
is recorded in ``docs/decisions/locked-decisions.md``.
"""

from meho_backplane.connectors.gcloud.connector import GcloudConnector
from meho_backplane.connectors.gcloud.session import (
    GcloudCredentialsLoader,
    GcloudTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_connector_v2(
    product="gcloud",
    version="1.0",
    impl_id="gcloud-rest",
    cls=GcloudConnector,
)

# G0.15-T6 (#1215) wildcard fallback -- the K8s sibling pattern fanned
# out so a target with ``version=None`` (fresh, unfingerprinted, no
# operator-asserted version yet) resolves to this connector through
# the resolver's ``versioned_over_wildcard`` step rather than 501-ing
# with ``no_connector``. The versioned entry above always wins when
# both are present (resolver tie-break step 1).
register_connector_v2(
    product="gcloud",
    version="",
    impl_id="",
    cls=GcloudConnector,
)

# Queue the typed-op upsert onto the lifespan-driven registrar list.
# run_typed_op_registrars() (called during app lifespan startup) will
# invoke this after all connector modules have been eager-imported, so
# every connector's v2 registry entry exists before the op walk begins.
register_typed_op_registrar(GcloudConnector.register_gcloud_typed_operations)

__all__ = [
    "GcloudConnector",
    "GcloudCredentialsLoader",
    "GcloudTargetLike",
    "load_credentials_from_vault",
]
