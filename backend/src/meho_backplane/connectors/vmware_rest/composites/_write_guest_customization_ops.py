# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guest OS customization: save a customization spec and apply one to a VM.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._supervisor_library_llm_instructions import (
    SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    guest_customization_spec_create_composite,
    vm_customize_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _guest_customization_spec_create_preview,
    _vm_customize_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA,
    GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA,
    VM_CUSTOMIZE_PARAMETER_SCHEMA,
    VM_CUSTOMIZE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.guest.customization_spec.create",
        handler=guest_customization_spec_create_composite,
        summary="Save a named guest customization spec (host name, IP per NIC, DNS).",
        description=(
            "Saves a named guest customization spec in vCenter for a Linux or Windows guest: "
            "host name, a fixed IP or DHCP per NIC, and DNS. vm.customize or a clone applies "
            "it by name. Windows passwords and keys are used in the spec but never shown in "
            "a preview, broadcast or audit row."
        ),
        parameter_schema=GUEST_CUSTOMIZATION_SPEC_CREATE_PARAMETER_SCHEMA,
        response_schema=GUEST_CUSTOMIZATION_SPEC_CREATE_RESPONSE_SCHEMA,
        group_key="guest",
        tags=["composite", "write", "guest", "customization", "provisioning"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS[
            "vmware.composite.guest.customization_spec.create"
        ],
        preview=_guest_customization_spec_create_preview,
        governed_subops=_write._SUB_OPS_GUEST_CUSTOMIZATION_SPEC_CREATE,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.customize",
        handler=vm_customize_composite,
        summary="Apply a saved guest customization spec to a VM by name; power it on if asked.",
        description=(
            "Finds the VM by name and sets the saved customization spec on it, so the guest "
            "gets its host name and network settings on the next boot. A powered-on VM is "
            "refused (precondition_failed). With power_on it starts the VM right after."
        ),
        parameter_schema=VM_CUSTOMIZE_PARAMETER_SCHEMA,
        response_schema=VM_CUSTOMIZE_RESPONSE_SCHEMA,
        group_key="guest",
        tags=["composite", "write", "guest", "vm", "customization"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=SUPERVISOR_LIBRARY_LLM_INSTRUCTIONS["vmware.composite.vm.customize"],
        preview=_vm_customize_preview,
        governed_subops=_write._SUB_OPS_VM_CUSTOMIZE,
    ),
)
