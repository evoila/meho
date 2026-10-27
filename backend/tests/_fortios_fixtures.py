# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded FortiOS answers for the FortiGate connector tests.

Built from the answer shapes in Fortinet's public documentation and
community articles. Every name, serial and number here is made up. No secret
is in this file: the tests that plant secrets build them in the test, and
``test_saved_fixtures_hold_no_secret`` checks this file.
"""

from __future__ import annotations

import copy
from typing import Any

HOST = "fgt-device.test.invalid"
BASE_URL = f"https://{HOST}"
TOKEN = "made-up-api-token-value"
SERIAL_A = "FGT60FTK00000001"
SERIAL_B = "FGT60FTK00000002"
VM_SERIAL = "FGVMEVTEST000001"


def wrap(
    results: Any,
    *,
    path: str,
    name: str,
    version: str = "v7.4.4",
    serial: str = SERIAL_A,
    vdom: str = "root",
    action: str = "",
) -> dict[str, Any]:
    """A FortiOS success answer around *results*."""
    return {
        "http_method": "GET",
        "results": copy.deepcopy(results),
        "vdom": vdom,
        "path": path,
        "name": name,
        "action": action,
        "status": "success",
        "serial": serial,
        "version": version,
        "build": 2662,
    }


def error_answer(
    http_status: int, *, error: int = -3, cli_error: str | None = None
) -> dict[str, Any]:
    """A FortiOS error answer."""
    answer: dict[str, Any] = {
        "http_method": "GET",
        "status": "error",
        "http_status": http_status,
        "error": error,
        "serial": SERIAL_A,
        "version": "v7.4.4",
        "build": 2662,
    }
    if cli_error is not None:
        answer["cli_error"] = cli_error
    return answer


STATUS_HW = {
    "model_name": "FortiGate",
    "model_number": "60F",
    "model": "FGT60F",
    "hostname": "fgt-example-1",
    "log_disk_status": "available",
}
STATUS_VM = {
    "model_name": "FortiGate",
    "model_number": "VM64",
    "model": "FGVM64",
    "hostname": "fgt-example-vm",
    "log_disk_status": "available",
}
STATUS_NOT_FORTIGATE = {
    "model_name": "FortiWeb",
    "model_number": "VM",
    "model": "FWBVM",
    "hostname": "fwb-example",
    "log_disk_status": "available",
}
FIRMWARE = {
    "current": {"name": "FortiOS", "version": "v7.4.4", "build": 2662, "major": 7, "minor": 4},
    "available": [],
}
HA_PEERS_PAIR = [
    {
        "serial_no": SERIAL_B,
        "vcluster_id": 0,
        "priority": 100,
        "hostname": "fgt-example-2",
        "primary": False,
    },
    {
        "serial_no": SERIAL_A,
        "vcluster_id": 0,
        "priority": 200,
        "hostname": "fgt-example-1",
        "primary": True,
    },
]
HA_PEERS_SINGLE: list[dict[str, Any]] = []
CHECKSUMS_SAME = [
    {
        "is_manage_primary": 1,
        "is_root_primary": 1,
        "checksum": {"global": "aaa1", "root": "bbb1", "all": "ccc1"},
        "serial_no": SERIAL_A,
    },
    {
        "is_manage_primary": 0,
        "is_root_primary": 0,
        "checksum": {"global": "aaa1", "root": "bbb1", "all": "ccc1"},
        "serial_no": SERIAL_B,
    },
]
CHECKSUMS_DIFFER = copy.deepcopy(CHECKSUMS_SAME)
CHECKSUMS_DIFFER[1]["checksum"]["all"] = "ccc2"
HA_SETTINGS = [{"group-name": "example-cluster", "group-id": 7, "mode": "a-p"}]
VDOMS = [
    {"name": "root", "q_origin_key": "root", "short-name": "root", "vcluster-id": 0},
    {"name": "vdom-a", "q_origin_key": "vdom-a", "short-name": "vdom-a", "vcluster-id": 0},
]
FMG_FREE = {
    "mode": "normal",
    "server": "",
    "status": "down",
    "registration_status": "unknown",
}
FMG_CONTROLS = {
    "mode": "normal",
    "server": "fmg.test.invalid",
    "status": "up",
    "registration_status": "registered",
}
LICENSES = {
    "forticare": {
        "type": "cloud_service_status",
        "status": "registered",
        "account": "someone@example.invalid",
        "company": "Example Company",
        "support": {"hardware": {"status": "licensed", "expires": 1893456000}},
    },
    "vdom": {"type": "platform", "can_upgrade": True, "used": 2, "max": 10},
    "ips": {"type": "downloaded_fds_object", "status": "licensed", "expires": 1893456000},
}
USAGE = {
    "cpu": [{"current": 12, "historical": {}}],
    "mem": [{"current": 40, "historical": {}}],
    "disk": [{"current": 5, "historical": {}}],
    "session": [{"current": 1500, "historical": {}}],
    "setuprate": [{"current": 20, "historical": {}}],
}
ADDRESS = {
    "name": "host-a",
    "q_origin_key": "host-a",
    "type": "ipmask",
    "subnet": "<subnet-a>",
    "comment": "made-up host",
}
USAGE_OF_ADDRESS = {
    "currently_using": [
        {"path": "firewall", "name": "policy", "mkey": 3, "attribute": "dstaddr"},
        {"path": "firewall", "name": "addrgrp", "mkey": "group-a", "attribute": "member"},
    ]
}

#: (api path) -> results, for a healthy HA pair on FortiOS 7.4.
DEVICE_ANSWERS: dict[str, Any] = {
    "monitor/system/status": STATUS_HW,
    "monitor/system/firmware": FIRMWARE,
    "monitor/system/ha-peer": HA_PEERS_PAIR,
    "monitor/system/ha-checksums": CHECKSUMS_SAME,
    "cmdb/system/ha": HA_SETTINGS,
    "cmdb/system/vdom": VDOMS,
    "monitor/system/central-management/status": FMG_FREE,
    "monitor/license/status": LICENSES,
    "monitor/system/resource/usage": USAGE,
}


class FakeFortiGate:
    """A respx side effect: answers ``/api/v2/<path>`` from a dict and records each call.

    A value in *answers* is the ``results`` of a success answer, or an
    ``httpx.Response`` sent as it is, or an exception raised as it is.
    """

    def __init__(
        self,
        answers: dict[str, Any] | None = None,
        *,
        version: str = "v7.4.4",
        serial: str = SERIAL_A,
    ) -> None:
        self.answers = dict(DEVICE_ANSWERS if answers is None else answers)
        self.version = version
        self.serial = serial
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> Any:
        import httpx

        self.calls.append(request)
        api_path = request.url.path.removeprefix("/api/v2/")
        if api_path not in self.answers:
            return httpx.Response(404, json=error_answer(404, error=-3))
        answer = self.answers[api_path]
        if isinstance(answer, httpx.Response):
            return answer
        if isinstance(answer, BaseException):
            raise answer
        section, _, name = api_path.rpartition("/")
        return httpx.Response(
            200,
            json=wrap(
                answer,
                path=section,
                name=name,
                version=self.version,
                serial=self.serial,
                vdom=request.url.params.get("vdom", ""),
            ),
        )

    def paths(self) -> list[str]:
        return [c.url.path.removeprefix("/api/v2/") for c in self.calls]
