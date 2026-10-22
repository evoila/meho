# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Nested response-schema parts of the guest and performance reads of ``vmware-rest``.

These operations pass vSphere data objects through as vCenter sends them, so
the field names are the VI-JSON names (camelCase) of ``vi-json.yaml`` in
``vmware/vcf-api-specs``: ``GuestProcessInfo``, ``GuestNicInfo``,
``GuestStackInfo``, ``NetIpConfigInfo``, ``NetDnsConfigInfo``,
``NetIpRouteConfigInfo``, ``GuestFileAttributes`` (with its POSIX and Windows
subtypes), ``PerfMetricId`` and ``PerfEntityMetric``. Each object may also
carry its ``_typeName``. Read by :mod:`.schemas`.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "GUEST_FILE_ATTRIBUTES_SCHEMA",
    "GUEST_NIC_INFO_SCHEMA",
    "GUEST_PROCESS_INFO_SCHEMA",
    "GUEST_STACK_INFO_SCHEMA",
    "PERF_ENTITY_METRIC_SCHEMA",
    "PERF_METRIC_ID_SCHEMA",
]

_TYPE_NAME: Final[dict[str, Any]] = {
    "type": "string",
    "description": "vSphere data object type, for example 'GuestNicInfo'.",
}

#: ``GuestProcessInfo``: one process of ``ListProcessesInGuest``.
GUEST_PROCESS_INFO_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Process name."},
        "pid": {"type": "integer", "description": "Process ID."},
        "owner": {"type": "string", "description": "User that owns the process."},
        "cmdLine": {"type": "string", "description": "Full command line."},
        "startTime": {"type": "string", "description": "Start time (RFC 3339)."},
        "endTime": {
            "type": "string",
            "description": (
                "End time. Only for a program started with StartProgramInGuest, and only "
                "for about 5 minutes after it ends."
            ),
        },
        "exitCode": {
            "type": "integer",
            "description": "Exit code. Only when endTime is there.",
        },
        "_typeName": _TYPE_NAME,
    },
}

_IP_ADDRESS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "ipAddress": {"type": "string", "description": "IPv4 or IPv6 address."},
        "prefixLength": {"type": "integer", "description": "Network prefix length."},
        "origin": {
            "type": "string",
            "description": (
                "How it was set, e.g. 'manual' or 'dhcp' (NetIpConfigInfoIpAddressOrigin)."
            ),
        },
        "state": {
            "type": "string",
            "description": "Address state, e.g. 'preferred' (NetIpConfigInfoIpAddressStatus).",
        },
        "lifetime": {"type": "string", "description": "When the address expires (RFC 3339)."},
        "_typeName": _TYPE_NAME,
    },
}

_DNS_CONFIG_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "dhcp": {"type": "boolean", "description": "Whether DHCP sets the DNS settings."},
        "hostName": {"type": "string", "description": "Host name part of the DNS name."},
        "domainName": {"type": "string", "description": "Domain name part of the DNS name."},
        "ipAddress": {
            "type": "array",
            "items": {"type": "string"},
            "description": "DNS server addresses, in the order they are used.",
        },
        "searchDomain": {
            "type": "array",
            "items": {"type": "string"},
            "description": "DNS search domains, in order.",
        },
        "_typeName": _TYPE_NAME,
    },
    "description": "DNS settings (NetDnsConfigInfo).",
}

_DHCP_CONFIG_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "DHCP client settings (NetDhcpConfigInfo): ipv4 and ipv6.",
}

#: ``GuestNicInfo``: one network adapter as VMware Tools reports it.
GUEST_NIC_INFO_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "network": {
            "type": "string",
            "description": "Portgroup or distributed port the adapter is connected to.",
        },
        "macAddress": {"type": "string", "description": "MAC address."},
        "connected": {"type": "boolean", "description": "Whether the adapter is connected."},
        "deviceConfigId": {
            "type": "integer",
            "description": "Key of the matching virtual network device of the VM.",
        },
        "ipAddress": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IP addresses (old field; prefer ipConfig.ipAddress).",
        },
        "ipConfig": {
            "type": "object",
            "properties": {
                "ipAddress": {
                    "type": "array",
                    "items": _IP_ADDRESS_SCHEMA,
                    "description": "Addresses with prefix length, origin and state.",
                },
                "dhcp": _DHCP_CONFIG_SCHEMA,
                "autoConfigurationEnabled": {
                    "type": "boolean",
                    "description": "Whether IPv6 router advertisement setup is on.",
                },
                "_typeName": _TYPE_NAME,
            },
            "description": "IP settings of the adapter (NetIpConfigInfo).",
        },
        "dnsConfig": _DNS_CONFIG_SCHEMA,
        "netBIOSConfig": {
            "type": "object",
            "description": "NetBIOS settings (NetBIOSConfigInfo), Windows guests only.",
        },
        "_typeName": _TYPE_NAME,
    },
}

#: ``GuestStackInfo``: one IP stack (routes, DNS, DHCP) as VMware Tools reports it.
GUEST_STACK_INFO_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "dnsConfig": _DNS_CONFIG_SCHEMA,
        "ipRouteConfig": {
            "type": "object",
            "properties": {
                "ipRoute": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "network": {
                                "type": "string",
                                "description": "Destination network, e.g. '0.0.0.0'.",
                            },
                            "prefixLength": {
                                "type": "integer",
                                "description": "Prefix length of the destination network.",
                            },
                            "gateway": {
                                "type": "object",
                                "properties": {
                                    "ipAddress": {
                                        "type": "string",
                                        "description": "Next-hop address.",
                                    },
                                    "device": {
                                        "type": "string",
                                        "description": "Device the route uses.",
                                    },
                                    "_typeName": _TYPE_NAME,
                                },
                                "description": "Where packets for this route go.",
                            },
                            "_typeName": _TYPE_NAME,
                        },
                    },
                    "description": "Routing table, all address families.",
                },
                "_typeName": _TYPE_NAME,
            },
            "description": "Routing table (NetIpRouteConfigInfo).",
        },
        "ipStackConfig": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Setting key."},
                    "value": {"type": "string", "description": "Setting value."},
                },
            },
            "description": "Kernel IP settings as key-value pairs.",
        },
        "dhcpConfig": _DHCP_CONFIG_SCHEMA,
        "_typeName": _TYPE_NAME,
    },
}

#: ``GuestFileAttributes`` of ``FileTransferInformation``, with the
#: ``GuestPosixFileAttributes`` and ``GuestWindowsFileAttributes`` fields.
GUEST_FILE_ATTRIBUTES_SCHEMA: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "properties": {
        "modificationTime": {"type": "string", "description": "Last change (RFC 3339)."},
        "accessTime": {"type": "string", "description": "Last access (RFC 3339)."},
        "symlinkTarget": {
            "type": "string",
            "description": "Target of a symbolic link (Linux guests).",
        },
        "ownerId": {"type": "integer", "description": "POSIX owner ID."},
        "groupId": {"type": "integer", "description": "POSIX group ID."},
        "permissions": {"type": "integer", "description": "POSIX permission bits."},
        "hidden": {"type": "boolean", "description": "Windows: the file is hidden."},
        "readOnly": {"type": "boolean", "description": "Windows: the file is read-only."},
        "createTime": {"type": "string", "description": "Windows: created (RFC 3339)."},
        "_typeName": {
            "type": "string",
            "description": "'GuestPosixFileAttributes' or 'GuestWindowsFileAttributes'.",
        },
    },
}

#: ``PerfMetricId``: one counter of ``QueryAvailablePerfMetric``.
PERF_METRIC_ID_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "counterId": {
            "type": "integer",
            "description": "Counter number; PerformanceManager.perfCounter gives its name.",
        },
        "instance": {
            "type": "string",
            "description": "Device the metric is for, e.g. a CPU or disk; '' is the whole entity.",
        },
        "_typeName": _TYPE_NAME,
    },
}

#: ``PerfEntityMetric``: the ``QueryPerf`` answer for one entity.
PERF_ENTITY_METRIC_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "entity": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "vim type, e.g. 'HostSystem'."},
                "value": {"type": "string", "description": "Moid, e.g. 'host-21'."},
            },
            "description": "The entity the samples are for.",
        },
        "sampleInfo": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string", "description": "Sample time (RFC 3339)."},
                    "interval": {"type": "integer", "description": "Sample length in seconds."},
                },
            },
            "description": "Time and length of each sample, oldest first.",
        },
        "value": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {**PERF_METRIC_ID_SCHEMA, "description": "Counter and instance."},
                    "value": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "One value per entry of sampleInfo, in the same order.",
                    },
                },
            },
            "description": "One series per counter and instance (PerfMetricIntSeries).",
        },
        "_typeName": _TYPE_NAME,
    },
}
