# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SrmConnector -- hand-written HttpConnector subclass for Site Recovery Manager.

Covers SRM 8.8, Live Site Recovery 9.0 and the recovery API of VCF 9.1
Protection and Recovery, all through the SRM REST API v2 at
``/api/rest/srm/v2``.

The login, the session cache, the version read and both operations are
shared with the vSphere Replication connector: both APIs are served by
the same REST API gateway
(:class:`~meho_backplane.connectors._shared.dr_rest.DrRestConnector`).
This class sets the SRM paths.

Login
-----

1. ``POST /api/rest/srm/v2/session`` with HTTP Basic: the vCenter single
   sign-on account of the SRM site, read from Vault through the
   :class:`~meho_backplane.connectors.srm.session.SrmSessionLoader`.
2. The JSON answer carries ``session_id``. It is cached per target and
   tenant (``target_cache_key``), so two targets on one address (a 9.1
   appliance registered once as ``srm`` and once as ``vr``) never share
   a session.
3. Every later call sends the id in the ``x-dr-session`` header.
4. On ``401`` from a later call the dispatcher calls
   ``invalidate_session``, which drops only this target's session, and
   dispatches once more; the next call logs in again.

Version
-------

``GET /api/rest/srm/v2/server/info`` returns the server's ``version`` and
``build_number``. ``server_version`` reads them and never raises; it
backs ``fingerprint`` and the ``srm.api_file.get`` answer. A ``401`` on
this read drops this target's cached session, as the dispatcher would for
an operation, so the next call logs in again instead of sending the
rejected session once more. The operation ``srm.about`` returns the whole
server summary.

Operations
----------

``srm.about`` and ``srm.api_file.get`` (body in
:mod:`meho_backplane.connectors._shared.dr_api_file`). Both exist on 8.8,
9.0 and 9.1, so no operation needs a version check yet.
"""

from __future__ import annotations

from meho_backplane.connectors._shared.dr_rest import DrRestConnector
from meho_backplane.connectors.srm.api_file import API_FILES
from meho_backplane.connectors.srm.session import (
    SrmSessionLoader,
    load_session_credentials_from_vault,
)

__all__ = ["API_BASE", "SrmConnector"]

#: Base path of the SRM REST API v2. The vendor file lists its paths
#: relative to it (``/session``, ``/server/info``).
API_BASE = "/api/rest/srm/v2"


class SrmConnector(DrRestConnector):
    """SRM REST API v2 connector with session-id auth (``x-dr-session``)."""

    product = "srm"
    version = "9.1"
    impl_id = "srm-rest"
    supported_version_range = ">=8.8,<10.0"
    priority = 1

    api_base = API_BASE
    session_path = f"{API_BASE}/session"
    info_path = f"{API_BASE}/server/info"
    api_files = API_FILES

    def __init__(self, *, session_loader: SrmSessionLoader | None = None) -> None:
        super().__init__(
            session_loader=(
                session_loader
                if session_loader is not None
                else load_session_credentials_from_vault
            )
        )
