# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Licence-management handlers for :class:`VcfOperationsConnector`.

VCF 9 licences live in VCF Operations. The calls below are the ones the VCF
Operations UI uses for them. They sit under ``/suite-api/internal/extension``
and are **not published** by the vendor: VCF Operations answers ``403`` to
them unless the request carries ``X-vRealizeOps-API-use-unsupported: true``.
Every handler sends that header.

The handlers sit on a mixin so the connector module stays small. The
dispatcher rebinds each handler to the live connector instance through
:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`, which
walks the connector's MRO (the same path the Prometheus read mixin uses).

Sources for the request shapes: the community PowerShell module
``lamw/Broadcom.Community.VCFLicensing`` (the assign body, the empty query
bodies, ``page`` / ``pageSize`` on the vCenter query) and live reads against
VCF Operations 9.1. The op metadata lives in
:mod:`~meho_backplane.connectors.vcf_operations.licensing_ops`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from meho_backplane.auth.operator import Operator

__all__ = ["UNSUPPORTED_API_HEADER", "VropsLicensingHandlers"]

#: VCF Operations refuses the ``internal`` calls without this header.
UNSUPPORTED_API_HEADER: dict[str, str] = {"X-vRealizeOps-API-use-unsupported": "true"}

_CLOUD_INTEGRATION = "/suite-api/internal/extension/vcf-license-cloud-integration"
_ENTITLEMENT = "/suite-api/internal/extension/vcf-entitlement"

_REGISTRATION_STATUS_PATH = f"{_CLOUD_INTEGRATION}/registration/status"
_LICENSE_SERVERS_PATH = f"{_CLOUD_INTEGRATION}/license-servers"
_ENTITLEMENTS_QUERY_PATH = f"{_ENTITLEMENT}/entitlements/query"
_VCENTER_SYSTEMS_QUERY_PATH = f"{_ENTITLEMENT}/vcenter-systems/query"
#: Sent as ``pageSize`` when the caller gives none, so one call returns all
#: vCenters of a normal install instead of only the vendor's first page.
VCENTER_QUERY_DEFAULT_PAGE_SIZE = 1000
_ENTITLEMENT_LICENSE_SERVERS_QUERY_PATH = f"{_ENTITLEMENT}/license-servers/query"
_ASSIGN_PATH = f"{_ENTITLEMENT}/assign"

if TYPE_CHECKING:
    # The concrete connector also inherits HttpConnector; naming it as the
    # base here only lets the type checker see the request seams.
    from meho_backplane.connectors.adapters.http import HttpConnector
    from meho_backplane.connectors.vcf_operations.session import VcfOperationsTargetLike

    _Base = HttpConnector
else:
    _Base = object


class VropsLicensingHandlers(_Base):
    """The six licence-management handlers of the VCF Operations connector."""

    async def _licensing_get(
        self, operator: Operator, target: VcfOperationsTargetLike, path: str
    ) -> Any:
        # Retried GET (the base seam retries connection errors and 5xx only).
        return await self._request_json(
            target, "GET", path, operator=operator, extra_headers=UNSUPPORTED_API_HEADER
        )

    async def _licensing_query(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        # The query calls take no filter body; send an empty JSON object.
        return await self._post_json(
            target,
            path,
            operator=operator,
            params=params,
            json={},
            extra_headers=UNSUPPORTED_API_HEADER,
        )

    async def licensing_registration_status(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.licensing.registration.status`` — GET ``registration/status``."""
        del params  # schema declares the param object empty
        payload = await self._licensing_get(operator, target, _REGISTRATION_STATUS_PATH)
        return {"payload": payload}

    async def licensing_server_list(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.licensing.server.list`` — GET ``license-servers``."""
        del params
        payload = await self._licensing_get(operator, target, _LICENSE_SERVERS_PATH)
        return {"payload": payload}

    async def licensing_entitlement_query(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.licensing.entitlement.query`` — POST ``entitlements/query``.

        Returned unchanged: its fields are known (see the response schema).
        """
        del params
        result: dict[str, Any] = await self._licensing_query(
            operator, target, _ENTITLEMENTS_QUERY_PATH
        )
        return result

    async def licensing_vcenter_query(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.licensing.vcenter.query`` — POST ``vcenter-systems/query``.

        ``page`` / ``pageSize`` ride as query params. With no ``pageSize``
        the handler sends ``VCENTER_QUERY_DEFAULT_PAGE_SIZE``. Returned unchanged.
        """
        query = {key: params[key] for key in ("page", "pageSize") if params.get(key) is not None}
        query.setdefault("pageSize", VCENTER_QUERY_DEFAULT_PAGE_SIZE)
        result: dict[str, Any] = await self._licensing_query(
            operator, target, _VCENTER_SYSTEMS_QUERY_PATH, params=query
        )
        return result

    async def licensing_server_query(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.licensing.server.query`` — POST ``vcf-entitlement/license-servers/query``."""
        del params
        payload = await self._licensing_query(
            operator, target, _ENTITLEMENT_LICENSE_SERVERS_QUERY_PATH
        )
        return {"payload": payload}

    async def licensing_assign(
        self,
        operator: Operator,
        target: VcfOperationsTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``vrops.licensing.assign`` — POST ``vcf-entitlement/assign``.

        Assigns licences to one vCenter. The vendor body is a JSON **array**
        of ``{vcenter: {id, adapterName, host}, allocationIds: [...]}``. The
        UI and the community module send the vCenter address as both
        ``adapterName`` and ``host``; this handler does the same.
        """
        host = params["vcenterHost"]
        body = [
            {
                "vcenter": {"id": params["vcenterId"], "adapterName": host, "host": host},
                "allocationIds": list(params["licenseIds"]),
            }
        ]
        # The base seam is typed for a JSON object; httpx sends a list as a
        # JSON array just the same.
        payload = await self._post_json(
            target,
            _ASSIGN_PATH,
            operator=operator,
            json=cast("dict[str, Any]", body),
            extra_headers=UNSUPPORTED_API_HEADER,
        )
        return {"payload": payload}
