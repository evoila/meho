# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The handler methods of the shared approval, policy, ABX and subscription ops.

:class:`VraSharedHandlers` is a mixin. The ``vra8`` and ``vcf_automation``
connectors both inherit it and implement :meth:`VraSharedHandlers.vra_gov_calls`
with their own login. The dispatcher binds a handler defined on a mixin to
the connector instance the resolver picked
(:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`), so
each method runs with the right connector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.vmware_shared import vra_extensibility as ext
from meho_backplane.connectors.vmware_shared import vra_governance as gov

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = ["VraSharedHandlers"]


class VraSharedHandlers:
    """Handlers of the nine shared ops; the metadata is in ``vra_*_ops``."""

    def vra_gov_calls(self, operator: Operator, target: Any) -> gov.VraGovCalls:
        """The connector's GET and send for *target*. Each connector implements it."""
        raise NotImplementedError

    async def vra_approval_list(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.approval.list``."""
        return await gov.approval_list(self.vra_gov_calls(operator, target), params)

    async def vra_approval_decide(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.approval.decide``."""
        return await gov.approval_decide(self.vra_gov_calls(operator, target), params)

    async def vra_policy_list(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.policy.list``."""
        return await gov.policy_list(self.vra_gov_calls(operator, target), params)

    async def vra_policy_set(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.policy.set``."""
        return await gov.policy_set(self.vra_gov_calls(operator, target), params)

    async def vra_abx_action_list(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.abx.action.list``."""
        return await ext.abx_action_list(self.vra_gov_calls(operator, target), params)

    async def vra_abx_action_run(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.abx.action.run``."""
        return await ext.abx_action_run(self.vra_gov_calls(operator, target), params)

    async def vra_abx_run_get(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.abx.run.get``."""
        return await ext.abx_run_get(self.vra_gov_calls(operator, target), params)

    async def vra_subscription_list(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.subscription.list``."""
        return await ext.subscription_list(self.vra_gov_calls(operator, target), params)

    async def vra_subscription_set(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``<prefix>.subscription.set``."""
        return await ext.subscription_set(self.vra_gov_calls(operator, target), params)
