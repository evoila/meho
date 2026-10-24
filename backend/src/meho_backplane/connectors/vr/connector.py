# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VrConnector -- hand-written HttpConnector subclass for vSphere Replication.

Covers vSphere Replication 8.8 and 9.0 and the replication API of VCF 9.1
Protection and Recovery, all through the vSphere Replication REST API v2
at ``/api/rest/vr/v2``.

The login, the session cache, the version read and both operations are
shared with the SRM connector: both APIs are served by the same REST API
gateway (:class:`~meho_backplane.connectors._shared.dr_rest.DrRestConnector`).
This class sets the vSphere Replication paths.

Login
-----

1. ``POST /api/rest/vr/v2/session`` with HTTP Basic: the vCenter single
   sign-on account of the vSphere Replication site, read from Vault.
2. The JSON answer carries ``session_id``. It is cached per target and
   tenant (``target_cache_key``), so on a 9.1 appliance registered once
   as ``srm`` and once as ``vr`` the two targets never share a session.
3. Every later call sends the id in the ``x-dr-session`` header.
4. On ``401`` from a later call the dispatcher calls
   ``invalidate_session``, which drops only this target's session, and
   dispatches once more; the next call logs in again.

Version
-------

``GET /api/rest/vr/v2/info`` returns the server's ``version`` and
``build_number``. ``server_version`` reads them and never raises; it
backs ``fingerprint`` and the ``vr.api_file.get`` answer. The operation
``vr.about`` returns the whole server summary.

Operations
----------

``vr.about`` and ``vr.api_file.get``. The entry methods are defined on
this class and call the shared code (``server_about`` on the base class
and :func:`~meho_backplane.connectors._shared.dr_api_file.dr_api_file_get`).
Defining them here, not on the base class, is what keeps a ``vr.*``
operation from running on an ``srm`` target: the stored handler names
this class, and the dispatcher binds it only to a ``VrConnector``. Both
operations exist on 8.8, 9.0 and 9.1, so no operation needs a version
check yet.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.dr_api_file import dr_api_file_get
from meho_backplane.connectors._shared.dr_rest import DrRestConnector, DrTargetLike

__all__ = ["API_BASE", "API_FILES", "VrConnector"]

#: Base path of the vSphere Replication REST API v2. The vendor file lists
#: its paths relative to it (``/session``, ``/info``).
API_BASE = "/api/rest/vr/v2"

#: The files ``vr.api_file.get`` can read, by input name.
API_FILES: dict[str, str] = {
    "vr": "/api/rest/vr/v2/open-api.yaml",
    "configure": "/api/rest/configure/v2/open-api.yaml",
}


class VrConnector(DrRestConnector):
    """vSphere Replication REST API v2 connector with session-id auth (``x-dr-session``)."""

    product = "vr"
    version = "9.1"
    impl_id = "vr-rest"
    supported_version_range = ">=8.8,<10.0"
    priority = 1

    api_base = API_BASE
    session_path = f"{API_BASE}/session"
    info_path = f"{API_BASE}/info"
    api_files = API_FILES

    async def about(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vr.about`` -- ``GET /api/rest/vr/v2/info``, reduced."""
        del params  # the schema declares no parameters
        return await self.server_about(operator, target)

    async def api_file_get(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``vr.api_file.get``; the body is :func:`dr_api_file_get` in ``_shared/dr_api_file``."""
        return await dr_api_file_get(self, operator, target, params)
