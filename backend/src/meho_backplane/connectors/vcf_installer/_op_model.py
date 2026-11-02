# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shape of one VCF Installer typed operation, shared by the op lists.

:mod:`.typed_ops` (bring-up and depot), :mod:`.typed_ops_lifecycle`
(discovery, cancel, appliance info, releases, spec read) and
:mod:`.typed_ops_system` (proxy, CEIP and system settings, certificate remove,
resource calculation) all build :class:`InstallerTypedOp` rows. The model lives
here so no list file has to import another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "EMPTY_PARAMS",
    "GROUP_BRINGUP",
    "GROUP_DEPOT",
    "GROUP_SYSTEM",
    "InstallerTypedOp",
    "instructions",
]

#: The operation groups the typed-op lists share.
GROUP_BRINGUP = "installer-bringup"
GROUP_DEPOT = "installer-depot"
GROUP_SYSTEM = "installer-system"


@dataclass(frozen=True)
class InstallerTypedOp:
    """Metadata for one Installer typed op registered at lifespan startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
    exposing the async handler. Mirrors
    :class:`~meho_backplane.connectors.sddc_manager.typed_ops.SddcTypedOp`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: The parameter schema of a read that takes no input.
EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


def instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
    parameter_hints: dict[str, str],
    preconditions: str | None = None,
    result_scalars: tuple[str, ...] | None = None,
    result_digest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``llm_instructions`` blob of one op."""
    built: dict[str, Any] = {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
        "parameter_hints": parameter_hints,
    }
    if preconditions is not None:
        built["preconditions"] = preconditions
    if result_scalars is not None:
        # #3084: the JSONFlux reduction hint — when the vendor object's
        # set-shaped field (validationChecks[] / sddcSubTasks[]) rides the
        # reduction, the listed scalar siblings survive on the inline
        # summary so the submit → poll loop stays drivable through the
        # governed surface (the reducer's ``_preserved_scalars`` contract).
        built["result_scalars"] = {"keys": list(result_scalars)}
    if result_digest is not None:
        # #3122: the row-digest hint — names the collection to reduce (so a
        # two-list SddcTask reduces sddcSubTasks[] instead of passing through
        # the reducer's multi-list detail-object exemption) and drives the
        # inline per-state counts / IN_PROGRESS names / FAILED-rows-with-errors
        # digest (the reducer's ``_row_digest`` contract).
        built["result_digest"] = dict(result_digest)
    return built
