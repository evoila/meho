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

``srm.about`` and ``srm.api_file.get`` call the shared code
(``server_about`` on the base class and
:func:`~meho_backplane.connectors.vmware_shared.dr_api_file.dr_api_file_get`).
The pairing, remote login, recovery plan, protection group, issue and
history reads call :mod:`meho_backplane.connectors.srm.recovery_reads`.
The entry methods are defined on this class, not on the base class: that
keeps an ``srm.*`` operation from running on a ``vr`` target, because the
stored handler names this class and the dispatcher binds it only to an
``SrmConnector``. Every operation exists on 8.8, 9.0 and 9.1, so no
operation needs a version check yet.
"""

from __future__ import annotations

import asyncio
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.srm import appliance as _appliance
from meho_backplane.connectors.srm import appliance_certificate as _certificate
from meho_backplane.connectors.srm import appliance_register as _register
from meho_backplane.connectors.srm import recovery_reads
from meho_backplane.connectors.srm import storage_adapter as _storage_adapter
from meho_backplane.connectors.srm.api_file import API_FILES
from meho_backplane.connectors.srm.session import (
    SrmSessionLoader,
    load_session_credentials_from_vault,
)
from meho_backplane.connectors.srm.setup_api import (
    SetupCredentialsLoader,
    SetupSecretLoader,
    load_param_basic_credentials,
    load_param_secret,
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
        #: Setup API sessions, by appliance address and admin Vault reference.
        self.setup_sessions: dict[tuple[str, ...], str] = {}
        self.setup_lock = asyncio.Lock()
        #: Reads a username and password named by an op param (a Vault reference).
        self.setup_credentials_loader: SetupCredentialsLoader = load_param_basic_credentials
        # Reads a secret named in the op params, for example the signed archive
        # URL of a storage replication adapter. A test swaps it.
        self.setup_secret_loader: SetupSecretLoader = load_param_secret

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

    # -- Appliance setup (the setup API, ``/api/rest/configure/v2``) -------------

    async def appliance_show(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.appliance.show``; the body is ``srm.appliance.appliance_show``."""
        return await _appliance.appliance_show(self, operator, target, params)

    async def appliance_task_get(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.appliance.task.get``; the body is ``srm.appliance.appliance_task_get``."""
        return await _appliance.appliance_task_get(self, operator, target, params)

    async def appliance_register(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.appliance.register``; the body is in ``srm.appliance_register``."""
        return await _register.appliance_register(self, operator, target, params)

    async def appliance_network_set(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.appliance.network.set``; the body is ``srm.appliance.appliance_network_set``."""
        return await _appliance.appliance_network_set(self, operator, target, params)

    async def appliance_time_set(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.appliance.time.set``; the body is ``srm.appliance.appliance_time_set``."""
        return await _appliance.appliance_time_set(self, operator, target, params)

    async def appliance_csr_create(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.appliance.csr.create``; the body is in ``srm.appliance_certificate``."""
        return await _certificate.appliance_csr_create(self, operator, target, params)

    async def appliance_certificate_set(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.appliance.certificate.set``; the body is in ``srm.appliance_certificate``."""
        return await _certificate.appliance_certificate_set(self, operator, target, params)

    async def storage_adapter_install(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.storage_adapter.install``; the body is in ``srm.storage_adapter``."""
        return await _storage_adapter.storage_adapter_install(self, operator, target, params)

    async def aclose(self) -> None:
        """Clear the setup API sessions too, then the base clean-up."""
        async with self.setup_lock:
            self.setup_sessions.clear()
        await super().aclose()

    async def pairing_list(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.pairing.list`` -- ``GET /pairings``, one row per pairing."""
        del params  # the schema declares no parameters
        return await recovery_reads.pairing_list(self, operator, target)

    async def remote_login(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.remote_login`` -- ``POST /pairings/{pairing_id}/remote-session``."""
        return await recovery_reads.remote_login(self, operator, target, params)

    async def plan_list(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.plan.list`` -- the recovery plans of a pairing."""
        return await recovery_reads.plan_list(self, operator, target, params)

    async def plan_get(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.plan.get`` -- one plan with its groups, steps and VMs."""
        return await recovery_reads.plan_get(self, operator, target, params)

    async def group_list(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.group.list`` -- the protection groups, or the VMs of one group."""
        return await recovery_reads.group_list(self, operator, target, params)

    async def issue_list(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.issue.list`` -- the issues of a pairing, plan, group or SRM server."""
        return await recovery_reads.issue_list(self, operator, target, params)

    async def history_list(
        self, operator: Operator, target: DrTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``srm.history.list`` -- the run history of a plan, or one report."""
        return await recovery_reads.history_list(self, operator, target, params)
