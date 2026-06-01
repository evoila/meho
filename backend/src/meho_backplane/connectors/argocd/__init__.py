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
the way Harbor (#621) and bind9 (#367) do — is deliberately **not** queued
here. G3.12-T1 ships the substrate (connector class + bearer-token
credential loader + fingerprint + dual registration) with zero operations;
the curated read core (``argocd.app.list`` / ``argocd.app.get`` /
``argocd.app.diff`` / ``argocd.app.resource_tree`` /
``argocd.appproject.list`` / ``argocd.repo.list``) and its registrar arrive
in G3.12-T2. Queuing an empty registrar now would add a no-op startup hook
with nothing to upsert.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is intentionally **not** called: ArgoCD has no v1 chassis history,
and the v1 entry would land as ``("argocd", "", "")`` and confuse the
resolver tie-break ladder. Only the v2 triple (+ wildcard) advertises this
class — the same decision Harbor, bind9, NSX, and SDDC Manager made.
"""

from meho_backplane.connectors.argocd.connector import ArgoCdConnector
from meho_backplane.connectors.argocd.session import (
    ARGOCD_TOKEN_FIELD,
    ArgoCdCredentialsLoader,
    ArgoCdTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2

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

__all__ = [
    "ARGOCD_TOKEN_FIELD",
    "ArgoCdConnector",
    "ArgoCdCredentialsLoader",
    "ArgoCdTargetLike",
    "load_credentials_from_vault",
]
