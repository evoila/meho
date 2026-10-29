# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A small fake phpIPAM API for the phpIPAM connector tests.

It answers the calls the connector makes the way phpIPAM 1.8.2 does (the
``{code, success, message?, data?}`` shape, 404 for "nothing found", 409 for a
taken address) and keeps the reserved addresses, so a test can run a change
twice. Documentation addresses (``192.0.2.0/24``, ``2001:db8::/120``) and
made-up names only.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

APP_ID = "meho"
HOST = "ipam.test.invalid"
BASE_URL = f"https://{HOST}"


def _answer(status: int, data: Any = None, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {"code": status, "success": status < 400, **extra}
    if data is not None:
        body["data"] = data
    return httpx.Response(status, json=body)


def _error(status: int, message: str) -> httpx.Response:
    return _answer(status, message=message)


@dataclass
class FakePhpipam:
    """In-memory phpIPAM. ``calls`` records ``(method, path, json body or None)``."""

    app_token: str | None = "app-code-fake-0001"
    username: str | None = None
    password: str | None = None
    user_token: str = "user-token-fake-0001"
    subnets: dict[int, dict[str, Any]] = field(default_factory=dict)
    children: dict[int, list[int]] = field(default_factory=dict)
    addresses: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    lose_write: bool = False
    readback_hostname: str | None = None
    refuse_tokens: int = 0
    _next_id: int = 100

    def add_subnet(self, subnet_id: int, cidr: str, **fields: Any) -> None:
        network = ipaddress.ip_network(cidr)
        self.subnets[subnet_id] = {
            "id": str(subnet_id),
            "subnet": str(network.network_address),
            "mask": str(network.prefixlen),
            "sectionId": "1",
            "description": f"example net {subnet_id}",
            "isFolder": "0",
            "isFull": "0",
            "isPool": "0",
            **fields,
        }

    def add_address(self, subnet_id: int, ip: str, hostname: str | None) -> None:
        self._next_id += 1
        self.addresses[(subnet_id, ip)] = {
            "id": str(self._next_id),
            "subnetId": str(subnet_id),
            "ip": ip,
            "hostname": hostname,
            "description": None,
            "note": None,
        }

    # ------------------------------------------------------------------

    def calls_of(self, method: str, prefix: str = "") -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] == method and call[1].startswith(prefix)]

    def _authorized(self, request: httpx.Request) -> bool:
        token = request.headers.get("phpipam-token")
        if self.refuse_tokens > 0:
            self.refuse_tokens -= 1
            return False
        if self.app_token is not None:
            return token == self.app_token
        return token == self.user_token

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        prefix = f"/api/{APP_ID}"
        if not path.startswith(prefix + "/"):
            return _error(400, "Invalid application id")
        rest = path[len(prefix) :]
        if rest == "/user/":
            return self._user(request)
        if not self._authorized(request):
            return _error(401, "Unauthorized")
        return self._route(method, rest, body)

    def _user(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return _error(401, "Please provide token")
        expected = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return _error(500, "Invalid username or password")
        return _answer(200, {"token": self.user_token, "expires": "2026-10-29 18:00:00"})

    def _route(self, method: str, rest: str, body: Any) -> httpx.Response:
        if method == "GET" and rest == "/subnets/":
            return _answer(200, list(self.subnets.values()))
        if match := re.fullmatch(r"/subnets/cidr/([^/]+)/(\d+)/", rest):
            subnet, mask = unquote(match.group(1)), match.group(2)
            found = [s for s in self.subnets.values() if (s["subnet"], s["mask"]) == (subnet, mask)]
            return _answer(200, found) if found else _error(404, "No subnets found")
        if match := re.fullmatch(r"/subnets/(\d+)/(usage/|slaves/|addresses/)?", rest):
            return self._subnet(int(match.group(1)), match.group(2))
        if method == "GET" and (match := re.fullmatch(r"/subnets/(\d+)/addresses/([^/]+)/", rest)):
            return self._subnet_address(int(match.group(1)), unquote(match.group(2)))
        if match := re.fullmatch(r"/addresses/first_free/(\d+)/", rest):
            return self._first_free(method, int(match.group(1)), body)
        if method == "POST" and rest == "/addresses/":
            return self._create(int(body["subnetId"]), body["ip"], body)
        if match := re.fullmatch(r"/addresses/([^/]+)/(\d+)/", rest):
            return self._address(method, unquote(match.group(1)), int(match.group(2)))
        return _error(400, "Invalid request")

    def _subnet(self, subnet_id: int, part: str | None) -> httpx.Response:
        subnet = self.subnets.get(subnet_id)
        if subnet is None:
            return _error(400, f"Invalid subnet Id ({subnet_id})")
        if part is None:
            return _answer(200, subnet)
        if part == "slaves/":
            kids = [self.subnets[c] for c in self.children.get(subnet_id, [])]
            return _answer(200, kids) if kids else _error(404, "No slaves")
        used = [a for (sid, _ip), a in self.addresses.items() if sid == subnet_id]
        if part == "usage/":
            maxhosts = (
                ipaddress.ip_network(f"{subnet['subnet']}/{subnet['mask']}").num_addresses - 2
            )
            return _answer(
                200,
                {
                    "used": str(len(used)),
                    "freehosts": str(maxhosts - len(used)),
                    "maxhosts": str(maxhosts),
                },
            )
        return _answer(200, used) if used else _error(404, "No addresses found")

    def _free(self, subnet_id: int) -> str | None:
        subnet = self.subnets[subnet_id]
        network = ipaddress.ip_network(f"{subnet['subnet']}/{subnet['mask']}")
        for host in network.hosts():
            if (subnet_id, str(host)) not in self.addresses:
                return str(host)
        return None

    def _first_free(self, method: str, subnet_id: int, body: Any) -> httpx.Response:
        ip = self._free(subnet_id)
        if ip is None:
            return _error(404, "No free addresses found")
        if method == "GET":
            return _answer(200, ip)
        response = self._create(subnet_id, ip, body or {})
        if response.status_code == 201:
            return _answer(201, ip, message="Address created", id=self._next_id)
        return response

    def _create(self, subnet_id: int, ip: str, body: dict[str, Any]) -> httpx.Response:
        if self.lose_write:
            raise httpx.ReadTimeout("the answer was lost")
        if (subnet_id, ip) in self.addresses:
            return _error(409, "IP address already exists")
        self.add_address(subnet_id, ip, body.get("hostname"))
        row = self.addresses[(subnet_id, ip)]
        row["description"] = body.get("description")
        row["note"] = body.get("note")
        if self.readback_hostname is not None:
            row["hostname"] = self.readback_hostname
        return _answer(201, None, message="Address created", id=self._next_id)

    def _subnet_address(self, subnet_id: int, ip: str) -> httpx.Response:
        if subnet_id not in self.subnets:
            return _error(404, f"Invalid subnet Id ({subnet_id})")
        row = self.addresses.get((subnet_id, ip))
        return _answer(200, [row]) if row else _error(404, "No addresses found")

    def _address(self, method: str, ip: str, subnet_id: int) -> httpx.Response:
        row = self.addresses.get((subnet_id, ip))
        if method == "GET":
            # Like phpIPAM 1.8.2: this read finds only the first address row of
            # the subnet (lowest id). Later rows have no "ip" field there, so
            # phpIPAM answers 404 for them.
            in_subnet = [a for (sid, _ip), a in self.addresses.items() if sid == subnet_id]
            first = min(in_subnet, key=lambda a: int(a["id"]), default=None)
            return _answer(200, row) if row and row is first else _error(404, "No addresses found")
        if method == "DELETE":
            if self.lose_write:
                raise httpx.ReadTimeout("the answer was lost")
            if row is None:
                return _error(404, "No addresses found")
            if self.readback_hostname is None:
                del self.addresses[(subnet_id, ip)]
            return _answer(200, None, message="Address deleted")
        return _error(400, "Invalid request")
