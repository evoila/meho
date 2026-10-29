# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SrmConnector -- hand-written HttpConnector subclass for Site Recovery Manager.

Covers SRM 8.8, Live Site Recovery 9.0 and the recovery API of VCF 9.1
Protection and Recovery, all through the SRM REST API v2 at
``/api/rest/srm/v2``.

The login, the session cache, the version read and both operations are
shared with the vSphere Replication connector: both APIs are served by
the same REST API gateway
(:class:`~meho_backplane.connectors.vmware_shared.dr_rest.DrRestConnector`).
This class sets the SRM paths.

Login
-----

1. ``POST /api/rest/srm/v2/session`` with HTTP Basic: the vCenter single
   sign-on account of the SRM site, read from Vault through the
   :class:`~meho_backplane.connectors.srm.session.SrmSessionLoader`.
2. The JSON answer carries ``session_id``. It is cached per credential
   (``credential_cache_key``: tenant, secret and login host), so two
   ``srm`` targets that share one credential share one session. The
   ``srm`` and ``vr`` connectors keep separate caches, so a 9.1 appliance
   registered once as ``srm`` and once as ``vr`` keeps two sessions.
3. Every later call sends the id in the ``x-dr-session`` header.
4. On ``401`` from a later call the dispatcher calls
   ``invalidate_session`` with the rejected request. The session is
   dropped only when that request sent it, so a late ``401`` never drops
   a newer session. The dispatcher then runs the call once more.

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

``srm.about`` and ``srm.api_file.get``. The entry methods are defined on
this class and call the shared code (``server_about`` on the base class
and :func:`~meho_backplane.connectors.vmware_shared.dr_api_file.dr_api_file_get`).
Defining them here, not on the base class, is what keeps an ``srm.*``
operation from running on a ``vr`` target: the stored handler names this
class, and the dispatcher binds it only to an ``SrmConnector``. Both
operations exist on 8.8, 9.0 and 9.1, so no operation needs a version
check yet.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.srm.api_file import API_FILES
from meho_backplane.connectors.srm.session import (
    SrmSessionLoader,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.vmware_shared.dr_api_file import dr_api_file_get
from meho_backplane.connectors.vmware_shared.dr_rest import DrRestConnector, DrTargetLike

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

    async def about(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.about`` -- ``GET /api/rest/srm/v2/server/info``, reduced."""
        del params  # the schema declares no parameters
        return await self.server_about(operator, target)

    async def api_file_get(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.api_file.get``; the body is ``vmware_shared.dr_api_file.dr_api_file_get``."""
        return await dr_api_file_get(self, operator, target, params)
