# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The handler methods of the HCX Manager setup operations, as a mixin of ``HcxConnector``.

Each method is a thin shim over a body in :mod:`.manager_setup` or
:mod:`.manager_restart`. The mixin keeps these shims out of
``connector.py``; the dispatcher binds a mixin method like any other
method of the connector class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.hcx.session import HcxTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.hcx.connector import HcxConnector

__all__ = ["HcxManagerSetupOps"]


class HcxManagerSetupOps:
    """Handlers of ``hcx.manager.*``; mixed into ``HcxConnector``."""

    async def manager_config_show(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.config.show`` shim."""
        from meho_backplane.connectors.hcx.manager_setup import hcx_manager_config_show_impl

        return await hcx_manager_config_show_impl(_hcx(self), operator, target, params)

    async def manager_vcenter_set(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.vcenter.set`` shim."""
        from meho_backplane.connectors.hcx.manager_setup import hcx_manager_vcenter_set_impl

        return await hcx_manager_vcenter_set_impl(_hcx(self), operator, target, params)

    async def manager_nsx_set(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.nsx.set`` shim."""
        from meho_backplane.connectors.hcx.manager_setup import hcx_manager_nsx_set_impl

        return await hcx_manager_nsx_set_impl(_hcx(self), operator, target, params)

    async def manager_location_set(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.location.set`` shim."""
        from meho_backplane.connectors.hcx.manager_setup import hcx_manager_location_set_impl

        return await hcx_manager_location_set_impl(_hcx(self), operator, target, params)

    async def manager_role_mapping_set(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.role_mapping.set`` shim."""
        from meho_backplane.connectors.hcx.manager_setup import hcx_manager_role_mapping_set_impl

        return await hcx_manager_role_mapping_set_impl(_hcx(self), operator, target, params)

    async def manager_activate(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.activate`` shim."""
        from meho_backplane.connectors.hcx.manager_setup import hcx_manager_activate_impl

        return await hcx_manager_activate_impl(_hcx(self), operator, target, params)

    async def manager_restart(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.restart`` shim."""
        from meho_backplane.connectors.hcx.manager_restart import hcx_manager_restart_impl

        return await hcx_manager_restart_impl(_hcx(self), operator, target, params)

    async def manager_service_status(
        self, operator: Operator, target: HcxTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``hcx.manager.service.status`` shim."""
        from meho_backplane.connectors.hcx.manager_restart import hcx_manager_service_status_impl

        return await hcx_manager_service_status_impl(_hcx(self), operator, target, params)


def _hcx(instance: HcxManagerSetupOps) -> HcxConnector:
    """The mixin's instance, which is always an ``HcxConnector``."""
    return cast("HcxConnector", instance)
