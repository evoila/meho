# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connect-time address check for the Kubernetes API clients.

``kubernetes_asyncio`` builds its own :class:`aiohttp.TCPConnector`. This
module makes those clients use the shared connect-time check
(:mod:`meho_backplane.connectors._shared.dial_guard`):

* the connector checks every host right before it opens a new connection
  (:func:`~meho_backplane.connectors._shared.dial_guard.screen_connector_dials`):
  the API server, the host of a redirect the client follows, and a proxy
  host from the kubeconfig or from the environment, as an IP literal or as
  a name;
* the API server address is also checked once when the client is built, so
  a blocked server is refused before the first request.

This holds for the target's ``host`` and for a ``server`` the kubeconfig
names by itself. Through a proxy, MEHO checks the proxy address and the
destination address, and asks the proxy for a checked address of the
destination instead of its name. The TLS name check keeps the name.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import aiohttp

from meho_backplane.connectors._shared.dial_guard import (
    screen_connector_dials,
    screen_dial_addresses_async,
)

__all__ = ["check_api_client_dials"]


async def check_api_client_dials(api_client: Any) -> None:
    """Check the API server address of *api_client* and every host it will dial.

    Raises ``TargetDestinationBlockedError`` when the API server address is
    blocked or its name does not resolve. Test doubles that carry no real
    configuration or aiohttp connector are left as they are.
    """
    server = getattr(getattr(api_client, "configuration", None), "host", None)
    if isinstance(server, str):
        hostname = urlsplit(server).hostname
        if hostname:
            await screen_dial_addresses_async(hostname)
    session = getattr(getattr(api_client, "rest_client", None), "pool_manager", None)
    connector = getattr(session, "connector", None)
    if isinstance(connector, aiohttp.TCPConnector):
        screen_connector_dials(connector)
