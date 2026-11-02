# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``llm_instructions`` for the guest, event and performance operations of ``vmware-rest``.

One entry per op id, read by :mod:`._register`. Each entry has the keys the
operation contract check wants: ``when_to_call``, ``output_shape`` and
``next_step``, plus ``preconditions`` for every op that is not ``safe``.
``parameter_hints`` and ``result_scalars`` are optional. The op descriptions
stay short; the longer guidance lives here.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["GUEST_EVENT_PERF_LLM_INSTRUCTIONS"]

_P = "vmware.composite."

_GUEST_LOGIN = (
    "VMware Tools must run in the guest. The guest login (guest_username and "
    "guest_password) comes from the target's secret, never from params. If this "
    "VM has its own guest login stored under the target's secret namespace, pass "
    "its reference in guest_secret_ref; the value itself is never a parameter."
)

GUEST_EVENT_PERF_LLM_INSTRUCTIONS: Final[dict[str, dict[str, Any]]] = {
    _P + "event.tail": {
        "when_to_call": (
            "Call to read the vCenter event log: who logged in, what was powered on or "
            "off, alarms, host problems. Filter by time, event type or one object. For "
            "the list of recent tasks and whether they finished, use vmware.tasks.recent."
        ),
        "parameter_hints": {
            "begin_time": "RFC 3339 time, e.g. '2026-10-19T08:00:00Z'.",
            "event_type_ids": "Event class names such as 'VmPoweredOnEvent'.",
            "entity": "{moId, type}, e.g. {moId: 'vm-42', type: 'VirtualMachine'}.",
        },
        "output_shape": (
            "{events: [{createdTime, eventTypeId, fullFormattedMessage, entity: {type, "
            "moId, name}}], count, moId, max_count_applied}. entity is null when the "
            "event names no object. A long list comes back as a result handle."
        ),
        "next_step": (
            "If count equals max_count_applied, there may be more events: narrow the time "
            "window or the event types and call again. Use an eventTypeId from a row as an "
            "event_type_ids value to find more events of that kind."
        ),
    },
    _P + "performance.summary": {
        "when_to_call": (
            "Call to get recent performance samples (CPU, memory, disk, network "
            "counters) of one VM, host or cluster. For a quick CPU and memory load of "
            "every host, vmware.host.usage is simpler."
        ),
        "parameter_hints": {
            "entity_moid": "e.g. 'vm-42', 'host-21' or 'domain-c7'.",
            "interval_seconds": "20 for hosts and VMs (real time); 300 for a cluster.",
        },
        "output_shape": (
            "{entity_moid, perf_manager_moid, available_counters: [{counterId, "
            "instance}], samples: [{entity, sampleInfo: [{timestamp, interval}], value: "
            "[{id: {counterId, instance}, value: [int]}]}], interval_seconds, "
            "max_samples_applied}. Counters are numbers only; instance '' is the whole "
            "entity."
        ),
        "next_step": (
            "To turn a counterId into a name and unit, read 'perfCounter' of the "
            "PerformanceManager with vmware.object.collect (type 'PerformanceManager', "
            "moid 'PerfMgr'). If samples is empty for a cluster, call again with "
            "interval_seconds 300."
        ),
    },
    _P + "vm.guest.env.read": {
        "when_to_call": (
            "Call to read environment variables inside a running VM's guest OS, for "
            "example PATH or a proxy setting."
        ),
        "preconditions": _GUEST_LOGIN,
        "parameter_hints": {"names": "Variable names; omit to read all of them."},
        "output_shape": (
            "{vm, process_manager_moid, variables: ['NAME=[REDACTED:secret_value]'], "
            "count}. The values are hidden; only the names show. A long list comes back "
            "as a result handle."
        ),
        "next_step": (
            "Split each entry at the first '=' to get the name. Only a person can pass "
            "reveal_secret: true to see the values. To change the environment of a "
            "program you start, use vm.guest.program.run with env."
        ),
    },
    _P + "vm.guest.file.read": {
        "when_to_call": (
            "Call to check that a file exists in a guest OS and read its size and "
            "attributes, or, with fetch_content=true, to read the file itself."
        ),
        "preconditions": (
            _GUEST_LOGIN + " The guest login must be allowed to read guest_path. The file "
            "is read as that login, often root or Administrator, so it can show secrets; "
            "for an agent the call waits for approval."
        ),
        "parameter_hints": {
            "guest_path": "Absolute path in the guest, e.g. '/etc/os-release'.",
            "max_inline_bytes": "Only with fetch_content=true; at most 8388608 (8 MiB).",
        },
        "output_shape": (
            "{vm, file_manager_moid, guest_path, size_bytes, attributes, content_fetch: "
            "deferred|inline}. deferred adds url (one-time download link). inline adds "
            "content_encoding: utf-8|base64 and content_lines: [str]; a long file comes "
            "back as a result handle."
        ),
        "next_step": (
            "With content_encoding utf-8, join content_lines with newlines. With base64, "
            "join the parts and decode them. A file over max_inline_bytes is refused, not "
            "cut: raise max_inline_bytes (up to 8 MiB) or read without fetch_content."
        ),
        # Keep the identifying and size fields on the reduced summary when a
        # large file spills content_lines to a result handle.
        "result_scalars": {
            "keys": [
                "vm",
                "file_manager_moid",
                "guest_path",
                "size_bytes",
                "content_fetch",
                "content_encoding",
            ]
        },
    },
    _P + "vm.guest.file.write": {
        "when_to_call": (
            "Call to put a text file into a running VM's guest OS, for example a config "
            "file, without copying it in another way."
        ),
        "preconditions": (
            _GUEST_LOGIN + " The guest login must be allowed to write guest_path. If the "
            "file exists and overwrite is false, vCenter refuses the write. A second "
            "person must approve, unless a standing grant covers it."
        ),
        "parameter_hints": {
            "content": "UTF-8 text. Never put a password or token here.",
        },
        "output_shape": (
            "{status: written, vm, file_manager_moid, guest_path, size_bytes, overwrite}. "
            "While approval is pending, the answer is the approval result "
            "(awaiting_approval or denied) instead."
        ),
        "next_step": (
            "To check the file, call vm.guest.file.read with fetch_content=true. To run a "
            "command that uses it, call vm.guest.program.run."
        ),
    },
    _P + "vm.guest.net.show": {
        "when_to_call": (
            "Call to see the network settings a guest OS reports (IP addresses, MAC, "
            "routes, DNS, gateways), for example when a VM cannot be reached. This guest "
            "read needs no guest login."
        ),
        "preconditions": "VMware Tools must run in the guest; it reports these values.",
        "output_shape": (
            "{vm, nics: [{network, macAddress, connected, deviceConfigId, ipConfig: "
            "{ipAddress: [{ipAddress, prefixLength, origin, state}]}, dnsConfig}], "
            "ip_stacks: [{dnsConfig: {hostName, domainName, ipAddress, searchDomain}, "
            "ipRouteConfig: {ipRoute: [{network, prefixLength, gateway: {ipAddress, "
            "device}}]}}]}. Both lists are empty when Tools reports nothing."
        ),
        "next_step": (
            "If nics is empty, check Tools with vmware.vm.info (tools_running_status). "
            "The IPv4 route with network '0.0.0.0' and prefixLength 0 is the default gateway."
        ),
    },
    _P + "vm.guest.process.list": {
        "when_to_call": (
            "Call to see which processes run inside a VM's guest OS, for example to check "
            "that a service started or to read the exit code of a program."
        ),
        "preconditions": _GUEST_LOGIN,
        "output_shape": (
            "{vm, process_manager_moid, processes: [{name, pid, owner, cmdLine, startTime, "
            "endTime, exitCode}], count, max_processes_applied}. A long list comes back "
            "as a result handle."
        ),
        "next_step": (
            "If count equals max_processes_applied, the list was cut: call again with a "
            "higher max_processes. endTime and exitCode are there only for programs "
            "started with vm.guest.program.run, for about 5 minutes after they end."
        ),
    },
    _P + "vm.guest.program.run": {
        "when_to_call": (
            "Call to run a command inside a running VM's guest OS, for example a "
            "systemctl or PowerShell command. Set wait=true to get the exit code."
        ),
        "preconditions": (
            _GUEST_LOGIN + " The guest login must be allowed to run program_path. No "
            "output is captured: to get it, redirect it to a file in arguments and read "
            "that file with vm.guest.file.read. env replaces the whole guest environment. "
            "A second person must approve, unless a standing grant covers it."
        ),
        "parameter_hints": {
            "program_path": "Absolute path, e.g. '/usr/bin/systemctl'.",
            "arguments": "One string, e.g. 'restart <service-name>'.",
        },
        "output_shape": (
            "{status: started|exited|timeout|exit_unknown, vm, process_manager_moid, "
            "program_path, pid, exit_code, start_time, end_time, wait, issues}. "
            "arguments and env are never returned. While approval is pending, the answer "
            "is the approval result (awaiting_approval or denied) instead."
        ),
        "next_step": (
            "On exited, check exit_code (0 usually means success). On timeout or "
            "started, the program may still run: find its pid in vm.guest.process.list "
            "to see when it ends and its exit code."
        ),
    },
}
