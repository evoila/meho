# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schema of the ``vmware.host.usage`` typed read.

The handler (:func:`~meho_backplane.connectors.vmware_rest.typed_ops.host_usage_impl`)
reshapes the vSphere ``HostListSummaryQuickStats`` and ``HostHardwareSummary``
objects into snake_case rows, so this schema follows the handler code. Kept
in its own module so ``typed_ops.py`` stays small.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["HOST_USAGE_RESPONSE_SCHEMA"]

_NOT_READ = "null when the host's property read failed (see read_note)"

HOST_USAGE_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "hosts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Host moid, e.g. 'host-21'."},
                    "name": {
                        "type": ["string", "null"],
                        "description": "Host name from the vCenter host list.",
                    },
                    "quick_stats": {
                        "type": ["object", "null"],
                        "properties": {
                            "overall_cpu_usage_mhz": {
                                "type": ["integer", "null"],
                                "description": "CPU in use now, in MHz (overallCpuUsage).",
                            },
                            "overall_memory_usage_mb": {
                                "type": ["integer", "null"],
                                "description": "Memory in use now, in MB (overallMemoryUsage).",
                            },
                            "uptime_seconds": {
                                "type": ["integer", "null"],
                                "description": "Time since the host started, in seconds.",
                            },
                        },
                        "description": f"Current load (summary.quickStats); {_NOT_READ}.",
                    },
                    "hardware": {
                        "type": ["object", "null"],
                        "properties": {
                            "cpu_model": {
                                "type": ["string", "null"],
                                "description": "CPU model name.",
                            },
                            "cpu_mhz": {
                                "type": ["integer", "null"],
                                "description": "Clock speed of one core, in MHz.",
                            },
                            "num_cpu_packages": {
                                "type": ["integer", "null"],
                                "description": "Number of CPU sockets.",
                            },
                            "num_cpu_cores": {
                                "type": ["integer", "null"],
                                "description": "Number of CPU cores of the host.",
                            },
                            "num_cpu_threads": {
                                "type": ["integer", "null"],
                                "description": "Number of CPU threads of the host.",
                            },
                            "memory_size_bytes": {
                                "type": ["integer", "null"],
                                "description": "Physical memory, in bytes (not MB).",
                            },
                        },
                        "description": f"Hardware totals (summary.hardware); {_NOT_READ}.",
                    },
                    "in_maintenance_mode": {
                        "type": ["boolean", "null"],
                        "description": f"Whether the host is in maintenance mode; {_NOT_READ}.",
                    },
                    "read_note": {
                        "type": "string",
                        "description": "Only when the host's property read failed: why.",
                    },
                },
                "required": ["id", "name"],
            },
            "description": "One row per ESXi host in scope.",
        },
    },
    "required": ["hosts"],
}
