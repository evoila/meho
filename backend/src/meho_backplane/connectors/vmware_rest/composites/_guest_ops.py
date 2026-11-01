# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The guest operations channel: reads inside a running VM's guest OS, a file
write and a program run.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._guest import (
    guest_env_read_composite,
    guest_file_read_composite,
    guest_file_write_composite,
    guest_net_show_composite,
    guest_process_list_composite,
    guest_program_run_composite,
)
from meho_backplane.connectors.vmware_rest.composites._guest_event_perf_llm_instructions import (
    GUEST_EVENT_PERF_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._guest_file_read_preview import (
    guest_file_read_preview,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _guest_file_write_preview,
    _guest_program_run_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    GUEST_ENV_READ_PARAMETER_SCHEMA,
    GUEST_ENV_READ_RESPONSE_SCHEMA,
    GUEST_FILE_READ_PARAMETER_SCHEMA,
    GUEST_FILE_READ_RESPONSE_SCHEMA,
    GUEST_FILE_WRITE_PARAMETER_SCHEMA,
    GUEST_FILE_WRITE_RESPONSE_SCHEMA,
    GUEST_NET_SHOW_PARAMETER_SCHEMA,
    GUEST_NET_SHOW_RESPONSE_SCHEMA,
    GUEST_PROCESS_LIST_PARAMETER_SCHEMA,
    GUEST_PROCESS_LIST_RESPONSE_SCHEMA,
    GUEST_PROGRAM_RUN_PARAMETER_SCHEMA,
    GUEST_PROGRAM_RUN_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.vm.guest.process.list",
        handler=guest_process_list_composite,
        summary="List the processes running in a VM's guest OS (needs VMware Tools).",
        description=(
            "Lists the processes in the guest OS (ListProcessesInGuest): name, pid, owner, "
            "command line, start time and, for programs started with vm.guest.program.run, "
            "end time and exit code. Uses the guest login from the target's secret. max_processes "
            "limits the list. Read-only."
        ),
        parameter_schema=GUEST_PROCESS_LIST_PARAMETER_SCHEMA,
        response_schema=GUEST_PROCESS_LIST_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "tools"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS[
            "vmware.composite.vm.guest.process.list"
        ],
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.guest.env.read",
        handler=guest_env_read_composite,
        summary="Read environment variables from a VM's guest OS (needs VMware Tools).",
        description=(
            "Reads the guest user's environment variables (ReadEnvironmentVariableInGuest). "
            "Omit names to read all of them. Uses the guest login from the target's secret. "
            "Returns the names; the values are hidden unless a person passes reveal_secret. "
            "Read-only."
        ),
        parameter_schema=GUEST_ENV_READ_PARAMETER_SCHEMA,
        response_schema=GUEST_ENV_READ_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "tools"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.env.read"],
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.guest.net.show",
        handler=guest_net_show_composite,
        summary="Show the network settings VMware Tools reports for a guest OS (no guest login).",
        description=(
            "Reads the guest network state VMware Tools reports on the VM: per NIC the IP "
            "addresses, MAC and connected flag (guest.net), and the routes, DNS and gateways "
            "(guest.ipStack). Needs no guest login and runs nothing in the guest. Read-only."
        ),
        parameter_schema=GUEST_NET_SHOW_PARAMETER_SCHEMA,
        response_schema=GUEST_NET_SHOW_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "networking"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.net.show"],
        # Unlike the other guest reads it sends no in-guest login, so it is not
        # a credential_write op (#3717).
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.guest.file.read",
        handler=guest_file_read_composite,
        summary="Read a guest file's size and attributes, or (opt-in) the file itself.",
        description=(
            "Starts a file download from the guest OS (InitiateFileTransferFromGuest). By default "
            "it returns size, attributes and a one-time download link. With fetch_content=true "
            "MEHO downloads the file and returns it as text or base64 parts, up to "
            "max_inline_bytes. Needs VMware Tools."
        ),
        parameter_schema=GUEST_FILE_READ_PARAMETER_SCHEMA,
        response_schema=GUEST_FILE_READ_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "read-only", "guest", "vi-json", "file"],
        # Caution, not safe (#3720): fetch_content=true returns arbitrary guest
        # file bytes read as the in-guest login (often privileged) with no
        # allow-list and no redaction, so the op must auto-park for agent /
        # service principals. requires_approval stays False -- caution parks for
        # non-human principals but executes immediately for a human seat, so an
        # operator's diagnostic read is unaffected. The tier is op-level, so even
        # a metadata-only fetch_content=false read parks for agents (the
        # conservative posture chosen in review over per-param tiering).
        safety_level="caution",
        requires_approval=False,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.file.read"],
        preview=guest_file_read_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.guest.file.write",
        handler=guest_file_write_composite,
        summary="Write a text file into a VM's guest OS (needs approval).",
        description=(
            "Writes UTF-8 text to guest_path in the guest OS: InitiateFileTransferToGuest gives "
            "a one-time upload link, then MEHO uploads the bytes. Uses the guest login from the "
            "target's secret. A second person must approve; the preview shows path and size, "
            "never the content."
        ),
        parameter_schema=GUEST_FILE_WRITE_PARAMETER_SCHEMA,
        response_schema=GUEST_FILE_WRITE_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "write", "guest", "vi-json", "file"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.file.write"],
        preview=_guest_file_write_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.guest.program.run",
        handler=guest_program_run_composite,
        summary="Run a program in a VM's guest OS (needs approval).",
        description=(
            "Starts a program in the guest OS (StartProgramInGuest) with the guest login from "
            "the target's secret. No output is captured. With wait=true it waits for the exit "
            "code, up to timeout_seconds. A second person must approve. arguments and env are "
            "never shown in the preview, the result or logs."
        ),
        parameter_schema=GUEST_PROGRAM_RUN_PARAMETER_SCHEMA,
        response_schema=GUEST_PROGRAM_RUN_RESPONSE_SCHEMA,
        group_key="guest_ops",
        tags=["composite", "write", "guest", "vi-json", "exec"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=GUEST_EVENT_PERF_LLM_INSTRUCTIONS["vmware.composite.vm.guest.program.run"],
        preview=_guest_program_run_preview,
    ),
)
