# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The handlers of the integration ops, and their registration on a connector.

Both Automation connector classes inherit :class:`VcfaIntegrationOpsMixin`
and set :attr:`~VcfaIntegrationOpsMixin.integration_ns`. Each one adds
``_integration_send`` (see
:class:`~.vcfa_integration_calls.IntegrationTransport`) and queues
:func:`register_integration_ops` with its own id prefix and group key.

Importing this module registers the preview builders of the two
``destructive`` ops (the remove and the IPAM package import).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from meho_backplane.connectors.vmware_shared.vcfa_git_sources import git_repo_set
from meho_backplane.connectors.vmware_shared.vcfa_integration_calls import (
    Calls,
    IntegrationTransport,
)
from meho_backplane.connectors.vmware_shared.vcfa_integration_delete import integration_delete
from meho_backplane.connectors.vmware_shared.vcfa_integration_ops import (
    GROUP_TEXT,
    INTEGRATION_OPS,
)
from meho_backplane.connectors.vmware_shared.vcfa_integration_schema import render
from meho_backplane.connectors.vmware_shared.vcfa_integration_set import integration_set
from meho_backplane.connectors.vmware_shared.vcfa_integrations import integration_list
from meho_backplane.connectors.vmware_shared.vcfa_ipam_package import ipam_package_import

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = ["VcfaIntegrationOpsMixin", "integration_op_ids", "register_integration_ops"]


def integration_op_ids(ns: str) -> tuple[str, ...]:
    """The ids of the five integration ops with the prefix *ns*."""
    return tuple(f"{ns}.{spec.name}" for spec in INTEGRATION_OPS)


class VcfaIntegrationOpsMixin:
    """The five integration op handlers. ``self`` is the connector."""

    #: The op id prefix of the connector (``vcfa.tenant`` or ``vcfa.vra8``).
    integration_ns: ClassVar[str]

    def _integration_calls(self, operator: Operator, target: Any) -> Calls:
        return Calls(
            connector=cast("IntegrationTransport", self),
            operator=operator,
            target=target,
            ns=self.integration_ns,
        )

    async def vcfa_integration_list(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``*.integration.list``."""
        return await integration_list(self._integration_calls(operator, target), params)

    async def vcfa_integration_set(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``*.integration.set``."""
        return await integration_set(self._integration_calls(operator, target), params)

    async def vcfa_integration_delete(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``*.integration.delete``."""
        return await integration_delete(self._integration_calls(operator, target), params)

    async def vcfa_ipam_package_import(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``*.ipam_package.import``."""
        return await ipam_package_import(self._integration_calls(operator, target), params)

    async def vcfa_git_repo_set(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``*.git_repo.set``."""
        return await git_repo_set(self._integration_calls(operator, target), params)


async def register_integration_ops(
    connector_cls: Any,
    *,
    group_key: str,
    plane_tag: str,
    embedding_service: EmbeddingService | None = None,
) -> int:
    """Register the five ops on *connector_cls* with its prefix; return the count."""
    from meho_backplane.operations.typed_register import register_typed_operation

    ns = connector_cls.integration_ns
    for spec in INTEGRATION_OPS:
        await register_typed_operation(
            product=connector_cls.product,
            version=connector_cls.version,
            impl_id=connector_cls.impl_id,
            op_id=f"{ns}.{spec.name}",
            handler=getattr(connector_cls, spec.handler_attr),
            summary=render(spec.summary, ns),
            description=render(spec.description, ns),
            parameter_schema=render(spec.parameter_schema, ns),
            response_schema=render(spec.response_schema, ns),
            group_key=group_key,
            when_to_use=render(GROUP_TEXT, ns),
            tags=[*spec.tags, plane_tag],
            safety_level=spec.safety_level,
            requires_approval=spec.requires_approval,
            llm_instructions=render(spec.llm_instructions, ns),
            embedding_service=embedding_service,
        )
    return len(INTEGRATION_OPS)
