# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shape of one Azure typed operation.

Every family module of the Azure connector builds its operations with
:class:`AzureTypedOp`. The class lives in its own module, so a family
module and :mod:`meho_backplane.connectors.azure.typed_ops` (which collects
the families) never import each other in a circle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["AzureTypedOp"]


@dataclass(frozen=True)
class AzureTypedOp:
    """One Azure typed operation, as the registrar passes it on.

    The fields match the keyword arguments of
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
    ``handler_attr`` names the method on ``AzureConnector`` that runs it.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None
