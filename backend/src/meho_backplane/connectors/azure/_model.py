# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces of the Azure operation tables.

Every family module of the Azure connector imports these:

* :class:`AzureTypedOp`: one operation, as the registrar passes it on.
* :data:`NO_PARAMS`: the parameter schema of an operation without parameters.
* :data:`TEXT_OR_NULL`: the JSON Schema type of a text field that may be null.

They live here and not in :mod:`meho_backplane.connectors.azure.typed_ops`,
because that module imports every family module. A family module that
imported it back would make an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = ["NO_PARAMS", "TEXT_OR_NULL", "AzureTypedOp"]


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


#: The parameter schema of an operation that takes no parameters.
NO_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

#: The JSON Schema ``type`` of a text field that may be null.
TEXT_OR_NULL: Final[list[str]] = ["string", "null"]
