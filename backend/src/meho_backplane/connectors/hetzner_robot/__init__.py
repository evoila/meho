# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.hetzner_robot — HetznerRobotConnector package.

Importing this package registers :class:`HetznerRobotConnector` against the
v2 connector registry under
``(product="hetzner-robot", version="2026-04", impl_id="hetzner-rest")``.

Registration is synchronous (import-time) only — this skeleton ships no typed
operations; all ops arrive in T8 via G0.7 spec ingestion of the Robot
Webservice OpenAPI spec into the ``endpoint_descriptor`` table.

The v1 :func:`~meho_backplane.connectors.registry.register_connector` entry
point is deliberately **not** called.  The connector advertises an explicit
``(version="2026-04", impl_id="hetzner-rest")`` key; the v1 entry would land
as ``("hetzner-robot", "", "")`` and confuse
:func:`~meho_backplane.connectors.resolver.resolve_connector`'s tie-break
ladder.  Same pattern :mod:`meho_backplane.connectors.harbor` established.
"""

from meho_backplane.connectors.hetzner_robot.connector import HetznerRobotConnector
from meho_backplane.connectors.hetzner_robot.session import (
    HetznerRobotCredentialsLoader,
    HetznerRobotTargetLike,
    SessionCredentials,
    load_credentials_from_vault,
)
from meho_backplane.connectors.registry import register_connector_v2

register_connector_v2(
    product="hetzner-robot",
    version="2026-04",
    impl_id="hetzner-rest",
    cls=HetznerRobotConnector,
)

__all__ = [
    "HetznerRobotConnector",
    "HetznerRobotCredentialsLoader",
    "HetznerRobotTargetLike",
    "SessionCredentials",
    "load_credentials_from_vault",
]
