# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connect-time address check for the Kubernetes API clients.

``kubernetes_asyncio`` builds its own :class:`aiohttp.TCPConnector`, and
aiohttp resolves the API server name again for every new connection. This
module makes those clients use the shared connect-time check
(:mod:`meho_backplane.connectors._shared.dial_guard`):

* the connector's resolver is replaced, so every new connection to a host
  name dials only addresses that passed the check;
* the API server address is checked once when the client is built, because
  aiohttp does not ask its resolver about an IP literal (an IP literal
  cannot change, so one check covers it).

This holds for the target's ``host`` and for a ``server`` the kubeconfig
names by itself.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import aiohttp

from meho_backplane.connectors._shared.dial_guard import (
    install_screening_resolver,
    screen_dial_addresses_async,
)

__all__ = ["check_api_client_dials"]


async def check_api_client_dials(api_client: Any) -> None:
    """Check the API server address of *api_client* and screen its new connections.

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
        install_screening_resolver(connector)
