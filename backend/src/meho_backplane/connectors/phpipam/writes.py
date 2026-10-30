# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The two phpIPAM changes: ``phpipam.address.reserve`` and ``phpipam.address.release``.

Both wait until a second person approves them (``requires_approval``). The
approval card shows the subnet, the address, the host name and whether the
address is free now (the preview builders at the end of this module).

Safe to run twice
-----------------

* Reserve: when the address is already reserved for the same host name
  (case and a trailing dot do not count), nothing is written and the result
  says ``nothing_to_do``. When another host name holds it, the call fails and
  names that host name. With ``first_free``, an address of the subnet that
  already has this host name counts as the reservation.
* Release: an address that is not reserved is ``nothing_to_do``.

After a write
-------------

The handler reads the address back and reports ``verification``:
``verified`` (the read-back shows the change), ``not_verified`` (it does
not) or ``unknown`` (the read-back failed). A failed read-back never hides
the change: the result still says what was written, and ``guidance`` says
that only the check failed. When phpIPAM refused the token during the
read-back, MEHO also forgets the cached secret, so a corrected secret works
on the next call without a restart. When the answer to the write is
lost (time-out, broken connection), the result is ``status: unknown`` and
``verification: unknown``, and MEHO makes no other call: the write is never
sent twice. Running the same reserve or release again is safe and shows what
happened.

Release never sends ``remove_dns``: DNS records stay with the DNS connectors.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.phpipam._common import (
    PhpipamRefusedError,
    Subnet,
    as_int,
    host_of,
    normalize_ip,
    read_address,
    read_subnet,
    same_host,
)
from meho_backplane.connectors.phpipam._errors import PhpipamAddressTakenError, PhpipamApiError
from meho_backplane.connectors.phpipam._paths import (
    _ADDRESS_PATH,
    _ADDRESSES_PATH,
    _FIRST_FREE_PATH,
)
from meho_backplane.connectors.phpipam.reads import used_addresses
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.phpipam.connector import PhpipamConnector
    from meho_backplane.connectors.phpipam.session import PhpipamTargetLike

__all__ = [
    "HOSTNAME_RE",
    "register_phpipam_preview_builders",
    "release_impl",
    "reserve_impl",
]

#: A DNS-style host name: labels of letters, digits and dashes, joined by dots.
HOSTNAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}\.?$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)

#: phpIPAM's ``description`` column holds 64 characters.
DESCRIPTION_MAX: Final[int] = 64
NOTE_MAX: Final[int] = 1000

_LOST_ANSWER: Final[str] = (
    "The answer from phpIPAM was lost, so MEHO does not know whether the change was made. "
    "MEHO did not send it again. Run the same call again: it is safe to run twice and shows "
    "the result."
)

_READBACK_FAILED: Final[str] = (
    "The change was made, but the read-back after it failed, so MEHO could not check it. "
    "Run the same call again: it is safe to run twice and shows the result."
)

_READBACK_REFUSED: Final[str] = (
    "The change was made, but phpIPAM refused the token when MEHO read the address back. "
    "MEHO forgot the cached secret. Check the secret of the target, then run the same call "
    "again: it is safe to run twice and shows the result."
)


def _subnet_id(params: dict[str, Any]) -> int:
    value = params.get("subnet_id")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PhpipamRefusedError("subnet_id must be the phpIPAM id of a subnet (1 or more)")
    return value


def _optional_text(params: dict[str, Any], name: str, cap: int) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > cap:
        raise PhpipamRefusedError(f"{name} must be a text of at most {cap} characters")
    return value


def _hostname(params: dict[str, Any]) -> str:
    value = params.get("hostname")
    if not isinstance(value, str) or not HOSTNAME_RE.fullmatch(value):
        raise PhpipamRefusedError(
            "hostname must be a host name such as web-01 or web-01.example.com "
            "(letters, digits, dashes, dots)"
        )
    return value


def _target_ip(params: dict[str, Any]) -> str | None:
    """The address the caller named, or ``None`` for the first free one."""
    ip = params.get("ip")
    first_free = params.get("first_free") is True
    if (ip is None) == (not first_free):
        raise PhpipamRefusedError(
            "name the address with 'ip', or set 'first_free': true -- exactly one of the two"
        )
    if ip is None:
        return None
    normalized = normalize_ip(ip)
    if normalized is None:
        raise PhpipamRefusedError(f"ip must be an IP address; got {ip!r}")
    return normalized


def _readback(row: dict[str, Any] | None, *, hostname: str | None, reserved: bool) -> str:
    """``verified`` / ``not_verified`` for a read-back row."""
    if reserved:
        return (
            "verified"
            if row is not None and same_host(host_of(row), hostname)
            else ("not_verified")
        )
    return "verified" if row is None else "not_verified"


async def _verify(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    subnet: Subnet,
    ip: str,
    *,
    hostname: str | None,
    reserved: bool,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Read the address back: ``(verification, row, guidance)``.

    A failed read gives ``unknown`` and a guidance text; it never raises,
    because the write before it was made and the result must say so.
    """
    try:
        row = await read_address(conn, operator, target, subnet.id, ip)
    except (httpx.TransportError, PhpipamApiError):
        return "unknown", None, _READBACK_FAILED
    except ConnectorAuthError:
        await conn.invalidate_credentials(target)
        return "unknown", None, _READBACK_REFUSED
    return _readback(row, hostname=hostname, reserved=reserved), row, None


def _reserve_result(subnet: Subnet, ip: str | None, hostname: str, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "op_class": "write",
        "action": "reserve",
        "status": "reserved",
        "subnet_id": subnet.id,
        "cidr": subnet.cidr,
        "ip": ip,
        "hostname": hostname,
        "address_id": None,
        "changed": True,
        "verification": "unknown",
        "guidance": None,
    }
    result.update(fields)
    return result


def _held(subnet: Subnet, ip: str, row: dict[str, Any] | None, hostname: str) -> dict[str, Any]:
    """The result for an address that is already reserved -- or the error if not for us."""
    held_by = host_of(row) if row is not None else None
    if row is not None and same_host(held_by, hostname):
        return _reserve_result(
            subnet,
            ip,
            hostname,
            status="nothing_to_do",
            changed=False,
            verification="verified",
            address_id=as_int(row.get("id")),
        )
    raise PhpipamAddressTakenError(ip=ip, subnet=str(subnet.cidr), held_by=held_by)


async def _existing_for_host(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    subnet: Subnet,
    hostname: str,
) -> dict[str, Any] | None:
    """The first address of *subnet* already reserved for *hostname*, if any."""
    for row in await used_addresses(conn, operator, target, subnet.id):
        if same_host(host_of(row), hostname) and normalize_ip(row.get("ip")):
            return row
    return None


def _reserve_body(params: dict[str, Any], hostname: str) -> dict[str, Any]:
    """The host name plus the optional description and note, checked."""
    body: dict[str, Any] = {"hostname": hostname}
    for name, cap in (("description", DESCRIPTION_MAX), ("note", NOTE_MAX)):
        text = _optional_text(params, name, cap)
        if text is not None:
            body[name] = text
    return body


async def _plan_reserve(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    subnet: Subnet,
    ip: str | None,
    body: dict[str, Any],
) -> dict[str, Any] | tuple[str, dict[str, object], dict[str, Any]]:
    """Return the result when the address is already reserved, else the write to send."""
    hostname = body["hostname"]
    if ip is not None:
        existing = await read_address(conn, operator, target, subnet.id, ip)
        if existing is not None:
            return _held(subnet, ip, existing, hostname)
        return _ADDRESSES_PATH, {}, {"subnetId": str(subnet.id), "ip": ip, **body}
    mine = await _existing_for_host(conn, operator, target, subnet, hostname)
    if mine is not None:
        return _held(subnet, str(normalize_ip(mine.get("ip"))), mine, hostname)
    return _FIRST_FREE_PATH, {"subnet_id": subnet.id}, body


async def reserve_impl(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``phpipam.address.reserve``: reserve one address with a host name and a note."""
    subnet_id = _subnet_id(params)
    hostname = _hostname(params)
    ip = _target_ip(params)
    body = _reserve_body(params, hostname)
    subnet = await read_subnet(conn, operator, target, subnet_id)
    if ip is not None:
        ip = str(subnet.check_address(ip))

    plan = await _plan_reserve(conn, operator, target, subnet, ip, body)
    if isinstance(plan, dict):
        return plan
    template, values, payload = plan
    try:
        answer = await conn.api_call(target, operator, "POST", template, values, json=payload)
    except httpx.TransportError:
        return _reserve_result(
            subnet, ip, hostname, status="unknown", changed=None, guidance=_LOST_ANSWER
        )
    except PhpipamApiError as exc:
        if exc.http_status != 409 or ip is None:
            raise
        # Someone reserved it between the check and the write.
        row = await read_address(conn, operator, target, subnet.id, ip)
        return _held(subnet, ip, row, hostname)
    return await _finish_reserve(conn, operator, target, subnet, ip, hostname, answer or {})


async def _finish_reserve(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    subnet: Subnet,
    ip: str | None,
    hostname: str,
    answer: dict[str, Any],
) -> dict[str, Any]:
    """Read the new reservation back and build the result."""
    if ip is None:
        ip = normalize_ip(answer.get("data"))
        if ip is None:
            return _reserve_result(
                subnet,
                None,
                hostname,
                status="unknown",
                changed=None,
                guidance="phpIPAM did not say which address it reserved. " + _LOST_ANSWER,
            )
    verification, row, guidance = await _verify(
        conn, operator, target, subnet, ip, hostname=hostname, reserved=True
    )
    address_id = as_int(answer.get("id"))
    if address_id is None and row is not None:
        address_id = as_int(row.get("id"))
    return _reserve_result(
        subnet, ip, hostname, verification=verification, address_id=address_id, guidance=guidance
    )


async def release_impl(
    conn: PhpipamConnector,
    operator: Operator,
    target: PhpipamTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``phpipam.address.release``: release one reserved address (no DNS change)."""
    subnet_id = _subnet_id(params)
    raw_ip = params.get("ip")
    ip = normalize_ip(raw_ip)
    if ip is None:
        raise PhpipamRefusedError(f"ip must be an IP address; got {raw_ip!r}")
    subnet = await read_subnet(conn, operator, target, subnet_id)
    if not subnet.contains(ip):
        raise PhpipamRefusedError(
            f"{ip} is not in subnet {subnet.cidr} (id {subnet.id}); nothing was changed"
        )
    result: dict[str, Any] = {
        "op_class": "write",
        "action": "release",
        "status": "released",
        "subnet_id": subnet.id,
        "cidr": subnet.cidr,
        "ip": ip,
        "released_hostname": None,
        "changed": True,
        "verification": "unknown",
        "guidance": None,
    }
    existing = await read_address(conn, operator, target, subnet.id, ip)
    if existing is None:
        return {**result, "status": "nothing_to_do", "changed": False, "verification": "verified"}
    result["released_hostname"] = host_of(existing)
    try:
        await conn.api_call(
            target,
            operator,
            "DELETE",
            _ADDRESS_PATH,
            {"ip": ip, "subnet_id": subnet.id},
            not_found_ok=True,
        )
    except httpx.TransportError:
        return {**result, "status": "unknown", "changed": None, "guidance": _LOST_ANSWER}
    verification, _row, guidance = await _verify(
        conn, operator, target, subnet, ip, hostname=None, reserved=False
    )
    return {**result, "verification": verification, "guidance": guidance}


# ---------------------------------------------------------------------------
# Approval previews: subnet, address, host name, and whether it is free now
# ---------------------------------------------------------------------------


def _params_preview(params: dict[str, Any]) -> dict[str, Any] | None:
    subnet_id = params.get("subnet_id")
    if not isinstance(subnet_id, int) or isinstance(subnet_id, bool):
        return None
    ip = normalize_ip(params.get("ip")) if params.get("ip") is not None else None
    return {
        "subnet_id": subnet_id,
        "cidr": None,
        "ip": ip,
        "first_free": params.get("first_free") is True,
        "hostname": params.get("hostname") if isinstance(params.get("hostname"), str) else None,
        "address_free": None,
        "held_by": None,
    }


async def _reserve_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    preview = _params_preview(ctx.params)
    if preview is None or ctx.connector_instance is None:
        # Without the connector there is no live read: the card shows the
        # request, and "free now" stays unknown.
        return preview
    conn: PhpipamConnector = ctx.connector_instance  # type: ignore[assignment]
    subnet = await read_subnet(conn, ctx.operator, ctx.target, preview["subnet_id"])
    preview["cidr"] = subnet.cidr
    if preview["ip"] is None and preview["first_free"]:
        answer = await conn.api_call(
            ctx.target,
            ctx.operator,
            "GET",
            _FIRST_FREE_PATH,
            {"subnet_id": subnet.id},
            not_found_ok=True,
        )
        preview["ip"] = normalize_ip((answer or {}).get("data"))
        preview["address_free"] = preview["ip"] is not None
        return preview
    if preview["ip"] is not None and subnet.contains(preview["ip"]):
        row = await read_address(conn, ctx.operator, ctx.target, subnet.id, preview["ip"])
        preview["address_free"] = row is None
        preview["held_by"] = host_of(row) if row is not None else None
    return preview


async def _release_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    preview = _params_preview(ctx.params)
    if preview is None:
        return None
    preview = {key: preview[key] for key in ("subnet_id", "cidr", "ip", "address_free", "held_by")}
    if ctx.connector_instance is None or preview["ip"] is None:
        return preview
    conn: PhpipamConnector = ctx.connector_instance  # type: ignore[assignment]
    subnet = await read_subnet(conn, ctx.operator, ctx.target, preview["subnet_id"])
    preview["cidr"] = subnet.cidr
    if subnet.contains(preview["ip"]):
        row = await read_address(conn, ctx.operator, ctx.target, subnet.id, preview["ip"])
        preview["address_free"] = row is None
        preview["held_by"] = host_of(row) if row is not None else None
    return preview


def register_phpipam_preview_builders() -> None:
    """Wire the approval-card previews of both changes."""
    register_preview_builder("phpipam.address.reserve", _reserve_preview)
    register_preview_builder("phpipam.address.release", _release_preview)


register_phpipam_preview_builders()
