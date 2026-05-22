# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.gcloud — GcloudConnector package.

Importing this package registers :class:`GcloudConnector` against the
v2 connector registry under
``(product="gcloud", version="v1", impl_id="gcloud-rest")``.

Registration is purely synchronous (import-time only): the v2 registry
entry lands via :func:`~meho_backplane.connectors.registry.register_connector_v2`.
No typed-op upserts are queued here — operations ship in G3.7-T5 (#848).

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called. The connector advertises an explicit
``(version="v1", impl_id="gcloud-rest")`` key; the v1 entry would land as
``("gcloud", "", "")`` and confuse the resolver's tie-break ladder. Same
pattern :mod:`meho_backplane.connectors.harbor` established.

Decision #12 (transport = B: HttpConnector + google-auth impersonation)
is recorded in ``docs/planning/v0.2-decisions.md``.
"""

from meho_backplane.connectors.gcloud.connector import GcloudConnector
from meho_backplane.connectors.gcloud.session import (
    GcloudCredentialsLoader,
    GcloudTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2

register_connector_v2(
    product="gcloud",
    version="v1",
    impl_id="gcloud-rest",
    cls=GcloudConnector,
)

__all__ = [
    "GcloudConnector",
    "GcloudCredentialsLoader",
    "GcloudTargetLike",
    "load_credentials_from_vault",
]
