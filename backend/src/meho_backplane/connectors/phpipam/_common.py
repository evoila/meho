# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Small helpers the phpIPAM reads and writes share.

phpIPAM sends numbers and flags as strings (``"id": "7"``, ``"isFull": "0"``),
so every field is converted here. Text from phpIPAM (descriptions, host
names) is data, never an instruction: it is cut and passed on, never acted on.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.phpipam._errors import HOSTNAME_CAP
from meho_backplane.connectors.phpipam._paths import _SUBNET_ADDRESS_PATH, _SUBNET_PATH

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.phpipam.connector import PhpipamConnector
    from meho_backplane.connectors.phpipam.session import PhpipamTargetLike

__all__ = [
    "PhpipamRefusedError",
    "Subnet",
    "as_bool",
    "as_int",
    "as_text",
    "first_row",
    "host_of",
    "normalize_ip",
    "read_address",
    "read_subnet",
    "rows",
    "same_host",
]

#: Descriptions from phpIPAM are cut to this many characters.
TEXT_CAP: Final[int] = 500

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class PhpipamRefusedError(ValueError):
    """MEHO refused the call before it changed anything (for example a wrong address)."""

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.remediation = remediation


def as_int(value: object) -> int | None:
    """phpIPAM's number (often a string) as ``int``, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def as_bool(value: object) -> bool:
    """phpIPAM's flag (``"1"``, ``1``, ``true``) as ``bool``."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def as_text(value: object, cap: int = TEXT_CAP) -> str | None:
    """phpIPAM's text, cut to *cap* characters; ``None`` when empty."""
    if value is None or isinstance(value, dict | list):
        return None
    text = str(value).strip()
    return text[:cap] if text else None


def rows(data: object) -> list[dict[str, Any]]:
    """The objects in a phpIPAM ``data`` field (one object or a list)."""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def first_row(data: object) -> dict[str, Any] | None:
    found = rows(data)
    return found[0] if found else None


def normalize_ip(value: object) -> str | None:
    """The address in its short standard form, or ``None`` when it is not one."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def same_host(left: str | None, right: str | None) -> bool:
    """Host names match without regard to case or a trailing dot."""
    if not left or not right:
        return False
    return left.strip().rstrip(".").lower() == right.strip().rstrip(".").lower()


def host_of(row: dict[str, Any]) -> str | None:
    """The host name of an address row, cut."""
    return as_text(row.get("hostname"), HOSTNAME_CAP)


@dataclass(frozen=True)
class Subnet:
    """One phpIPAM subnet, checked."""

    id: int
    network: IpNetwork | None
    description: str | None
    section_id: int | None
    is_folder: bool
    is_full: bool
    is_pool: bool

    @property
    def cidr(self) -> str | None:
        return str(self.network) if self.network is not None else None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Subnet | None:
        subnet_id = as_int(row.get("id"))
        if subnet_id is None:
            return None
        network: IpNetwork | None = None
        try:
            if row.get("subnet") and row.get("mask") not in (None, ""):
                network = ipaddress.ip_network(f"{row['subnet']}/{row['mask']}", strict=False)
        except ValueError:
            network = None
        return cls(
            id=subnet_id,
            network=network,
            description=as_text(row.get("description")),
            section_id=as_int(row.get("sectionId")),
            is_folder=as_bool(row.get("isFolder")),
            is_full=as_bool(row.get("isFull")),
            is_pool=as_bool(row.get("isPool")),
        )

    def usable_range(self) -> tuple[int, int]:
        """First and last address (as numbers) that can be given out.

        For IPv4 up to /30 phpIPAM keeps the network and broadcast address,
        unless the subnet is a pool. For IPv6 the network address
        (subnet-router anycast) is kept.
        """
        if self.network is None:
            raise PhpipamRefusedError(f"phpIPAM subnet {self.id} has no network address")
        first = int(self.network.network_address)
        last = int(self.network.broadcast_address)
        if self.network.version == 4:
            if self.network.prefixlen <= 30 and not self.is_pool:
                return first + 1, last - 1
            return first, last
        if self.network.prefixlen < 127:
            return first + 1, last
        return first, last

    def contains(self, ip: str) -> bool:
        """``True`` when *ip* is an address of this subnet (same IP version)."""
        if self.network is None:
            return False
        address = ipaddress.ip_address(ip)
        return address.version == self.network.version and address in self.network

    def address_at(self, number: int) -> str:
        """The address with this number, in the IP version of the subnet."""
        if self.network is None or self.network.version == 4:
            return str(ipaddress.IPv4Address(number))
        return str(ipaddress.IPv6Address(number))

    def check_address(self, ip: str) -> IpAddress:
        """Return *ip* if it can be given out in this subnet, else refuse."""
        if self.network is None:
            raise PhpipamRefusedError(f"phpIPAM subnet {self.id} has no network address")
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            raise PhpipamRefusedError(f"{ip!r} is not an IP address") from None
        if address.version != self.network.version or address not in self.network:
            raise PhpipamRefusedError(
                f"{address} is not in subnet {self.cidr} (id {self.id}); nothing was changed",
                remediation="Use an address from phpipam.subnet.free_addresses for this subnet.",
            )
        first, last = self.usable_range()
        if not first <= int(address) <= last:
            raise PhpipamRefusedError(
                f"{address} is the network or broadcast address of {self.cidr}; "
                "nothing was changed",
                remediation="Use an address from phpipam.subnet.free_addresses for this subnet.",
            )
        return address


async def read_subnet(
    conn: PhpipamConnector, operator: Operator, target: PhpipamTargetLike, subnet_id: int
) -> Subnet:
    """Read one subnet; refuse a folder or a subnet without a network address."""
    body = await conn.api_call(target, operator, "GET", _SUBNET_PATH, {"subnet_id": subnet_id})
    row = first_row((body or {}).get("data"))
    subnet = Subnet.from_row(row) if row is not None else None
    if subnet is None:
        raise PhpipamRefusedError(f"phpIPAM has no subnet with id {subnet_id}")
    if subnet.is_folder or subnet.network is None:
        raise PhpipamRefusedError(
            f"phpIPAM subnet {subnet_id} is a folder, not a subnet with addresses",
            remediation="Pick a subnet id from phpipam.subnet.list.",
        )
    return subnet


async def read_address(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    subnet_id: int,
    ip: str,
) -> dict[str, Any] | None:
    """Read one address of one subnet; ``None`` when it is not reserved."""
    body = await conn.api_call(
        target,
        operator,
        "GET",
        _SUBNET_ADDRESS_PATH,
        {"ip": ip, "subnet_id": subnet_id},
        not_found_ok=True,
    )
    if body is None:
        return None
    return first_row(body.get("data"))
