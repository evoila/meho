# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shape of one Azure typed operation, shared by every family module.

Each family of Azure operations (``inventory``, ``compute``, ...) lives in its
own module and builds its operations with :class:`AzureTypedOp`.
:mod:`meho_backplane.connectors.azure.typed_ops` collects them into one table.
The class sits here, not in ``typed_ops``, so a family module can import it
while ``typed_ops`` imports the family modules.
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
