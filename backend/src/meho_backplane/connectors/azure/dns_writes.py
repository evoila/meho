# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure public DNS writes, and their approval cards (#5368).

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.dns.DnsOps`. Both change a public
DNS name, so both wait for a person's approval.

* :func:`dns_record_set_delete_impl` deletes one record set. The call names
  the ``etag`` the approver saw. MEHO reads the record set first and refuses
  when its etag is another one, and Azure refuses too (``If-Match``). What is
  deleted is exactly the version on the approval card. The answer holds the
  deleted values (``previous``), and the audit row of the call keeps them as
  its state before, so a person can recreate the record set.
* :func:`dns_record_set_upsert_impl` creates a record set, or changes one when
  the call names its ``etag``. Without an etag it sends ``If-None-Match: *``,
  so it never overwrites a record set that exists. A change must name one
  etag (never ``*``), and it runs only as the approved run of a request whose
  card shows the current record set. The audit row keeps the old and the new
  values.

:func:`delete_card` and :func:`upsert_card` are the approval cards: they read
the record set as it is when the call waits for approval.

A record set is addressed by zone, type and name, all checked in
:mod:`.dns_parse`, inside the target's subscription.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._errors import AzureConflictError, AzureRequestError
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paths import DNS_API_VERSION
from meho_backplane.connectors.azure.dns_parse import record_set_body, record_set_path
from meho_backplane.connectors.azure.dns_reads import find_record_set
from meho_backplane.connectors.azure.dns_rows import record_set_detail
from meho_backplane.connectors.azure.dns_schemas import READ_RECORD_TYPES, WRITE_RECORD_TYPES
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config
from meho_backplane.operations._preview import PreviewContext, approved_preview

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "delete_card",
    "dns_record_set_delete_impl",
    "dns_record_set_upsert_impl",
    "upsert_card",
]

#: The types a delete takes: every type but SOA, which Azure deletes with the zone.
_DELETE_TYPES: Final[tuple[str, ...]] = tuple(t for t in READ_RECORD_TYPES if t != "SOA")

UPSERT_OP_ID: Final[str] = "azure.network.dns.recordset.upsert"

#: What the approver reads about a delete.
IRREVERSIBLE: Final[str] = (
    "permanent: Azure DNS keeps no copy of a deleted record set. Recreate it from the "
    "values on this card. Anyone may register or take over the name the record "
    "pointed to once it is gone."
)

#: What the approver reads when the record set is already gone.
ALREADY_ABSENT: Final[str] = (
    "none: the record set does not exist now, so the delete changes nothing."
)


class _WithHeaders:
    """The connector as :func:`send` sees it, with extra request headers.

    ``send`` asks its connector for the headers before every try, so a try
    after a 401 gets a new token and the same extra headers. The client, the
    trace and the 401 handling stay the connector's own.
    """

    def __init__(self, connector: AzureConnector, headers: dict[str, str]) -> None:
        self._connector = connector
        self._headers = headers

    async def authorize(
        self, target: AzureTargetLike, operator: Operator
    ) -> tuple[dict[str, str], tuple[str, str]]:
        headers, login = await self._connector.authorize(target, operator)
        return {**headers, **self._headers}, login

    async def invalidate_session(self, target: AzureTargetLike) -> None:
        await self._connector.invalidate_session(target)

    async def _http_client(self, target: AzureTargetLike) -> httpx.AsyncClient:
        return await self._connector._http_client(target)

    def _request_extensions(self, target: AzureTargetLike) -> dict[str, Any]:
        return self._connector._request_extensions(target)

    def _trace_response_body(self, start: Any, response: httpx.Response) -> Any:
        return self._connector._trace_response_body(start, response)


def _precondition_failed(target: AzureTargetLike, exc: AzureRequestError, hint: str) -> Exception:
    """The error for Azure's HTTP 412 (the etag did not match), else *exc* itself."""
    if exc.http_status != 412:
        return exc
    return AzureConflictError(
        f"Azure target {target.name!r}: the record set is not in the state the call "
        f"expected (HTTP 412), so MEHO changed nothing. {hint}",
        http_status=412,
        azure_error_code=exc.azure_error_code,
        azure_message=exc.azure_message,
        remediation=hint,
    )


async def _send_write(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    method: str,
    path: str,
    headers: dict[str, str],
    hint: str,
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    caller: Any = _WithHeaders(connector, headers)
    try:
        return await send(
            caller,
            target,
            method,
            path,
            operator=operator,
            params={"api-version": DNS_API_VERSION},
            json=body,
        )
    except AzureRequestError as exc:
        raise _precondition_failed(target, exc, hint) from exc


def _checked_for_delete(
    subscription_id: str, params: dict[str, Any]
) -> tuple[str, str, str, str, str]:
    """The path and names of a delete. Refuses the NS record set at the zone name."""
    path, group, zone, record_type, name = record_set_path(
        subscription_id, params, allowed_types=_DELETE_TYPES
    )
    if record_type == "NS" and name == "@":
        raise ValueError(
            "MEHO does not delete the NS record set at the zone name: Azure manages it "
            "and the zone stops answering without it."
        )
    return path, group, zone, record_type, name


def _etag_param(params: dict[str, Any]) -> str | None:
    etag = params.get("etag")
    if etag is None:
        return None
    if not isinstance(etag, str) or not etag.strip():
        raise ValueError("etag must be the etag text from azure.network.dns.recordset.show.")
    if any(part.strip() == "*" for part in etag.split(",")):
        raise ValueError(
            "etag must be one specific etag from azure.network.dns.recordset.show. "
            "A wildcard (*) matches any version, so it is refused."
        )
    return etag


def _bind_audit(**extras: Any) -> None:
    """Put old and new values on the audit row of this call (``audit_*`` keys).

    The dispatcher copies every bound ``audit_*`` value into the row as
    ``state_before`` and ``state_after``, and drops a value that is ``None``.
    """
    structlog.contextvars.bind_contextvars(**extras)


def _changed(target: AzureTargetLike, expected: str, current: str | None) -> Exception:
    hint = (
        "Read the record set again with azure.network.dns.recordset.show, check the "
        "values, and ask for a new approval with its etag."
    )
    return AzureConflictError(
        f"Azure target {target.name!r}: the record set changed after the approved version "
        f"(etag {expected!r}, now {current!r}), so MEHO changed nothing. {hint}",
        http_status=412,
        azure_error_code=None,
        azure_message=None,
        remediation=hint,
    )


async def dns_record_set_delete_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.dns.recordset.delete``: delete the approved version of one record set.

    Reads the record set. No such record set (HTTP 404, or Azure's 204 on the
    delete): nothing is deleted and ``already_absent`` is true. A different
    etag than the one in the call: nothing is deleted and the call fails.
    """
    path, group, zone, _type, _name = _checked_for_delete(
        resolve_target_config(target).subscription_id, params
    )
    etag = _etag_param(params)
    if etag is None:
        raise ValueError("etag is required: pass the etag from azure.network.dns.recordset.show.")
    base = {"zone": zone, "resource_group": group}
    current = await find_record_set(connector, operator, target, path)
    if current is None:
        return {**base, "deleted": False, "already_absent": True, "previous": None}
    if current.get("etag") != etag:
        raise _changed(target, etag, current.get("etag"))
    previous = record_set_detail(current)
    _bind_audit(audit_state_before=previous)
    response = await _send_write(
        connector,
        operator,
        target,
        "DELETE",
        path,
        {"If-Match": etag},
        "Read the record set again with azure.network.dns.recordset.show.",
    )
    gone = response.status_code == 204
    return {**base, "deleted": not gone, "already_absent": gone, "previous": previous}


def _approved_current(target: AzureTargetLike, etag: str | None) -> dict[str, Any] | None:
    """The record set on the approval card, or an error when there is no usable card.

    The upsert runs only as the approved run of a request. A card that is
    missing, or that has no read of the record set (the read failed while the
    card was built), means nobody saw the current values, so MEHO sends
    nothing. When the call names an etag, the card must show a record set
    with that etag.
    """
    card = approved_preview(UPSERT_OP_ID)
    current = (card or {}).get("current")
    if not isinstance(current, dict) or not isinstance(current.get("exists"), bool):
        raise ValueError(
            f"Azure target {target.name!r}: the approval request shows no current values of "
            "the record set (no card, or the read failed when it was built), so MEHO "
            "changed nothing. Send a new request."
        )
    if etag is not None and (current.get("exists") is not True or current.get("etag") != etag):
        raise ValueError(
            f"Azure target {target.name!r}: the approval card shows another version of the "
            f"record set than etag {etag!r}, so MEHO changed nothing. Send a new request."
        )
    return current if current["exists"] else None


async def dns_record_set_upsert_impl(
    connector: AzureConnector, operator: Operator, target: AzureTargetLike, params: dict[str, Any]
) -> dict[str, Any]:
    """``azure.network.dns.recordset.upsert``: create a record set, or change one by its etag."""
    path, group, zone, record_type, name = record_set_path(
        resolve_target_config(target).subscription_id, params, allowed_types=WRITE_RECORD_TYPES
    )
    body = record_set_body(params, record_type)
    etag = _etag_param(params)
    approved_current = _approved_current(target, etag)
    if etag is None:
        headers = {"If-None-Match": "*"}
        hint = (
            "A record set with this name and type exists. To change it, read it with "
            "azure.network.dns.recordset.show and pass its etag."
        )
    else:
        headers = {"If-Match": etag}
        hint = "Read the record set again with azure.network.dns.recordset.show."
    response = await _send_write(connector, operator, target, "PUT", path, headers, hint, body)
    payload = json_payload_or_empty(response)
    if not isinstance(payload.get("name"), str):
        # A success answer without a body: show what was sent, not nothing.
        payload = {"name": name, "type": f"Microsoft.Network/dnsZones/{record_type}", **body}
    detail = record_set_detail(payload)
    _bind_audit(audit_state_before=approved_current, audit_state_after=detail)
    return {
        "state": "created" if response.status_code == 201 else "updated",
        "zone": zone,
        "resource_group": group,
        "record_set": detail,
    }


# ----------------------------------------------------------------------
# The approval cards
# ----------------------------------------------------------------------


def _live_connector(ctx: PreviewContext) -> AzureConnector | None:
    """The live Azure connector, or ``None`` on the preview path that has none."""
    from meho_backplane.connectors.azure.connector import AzureConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, AzureConnector):
        return None
    return connector


def _card(
    record_type: str, group: str, zone: str, name: str, current: dict[str, Any] | None
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "kind": "azure_dns_record_set",
        "zone": zone,
        "resource_group": group,
        "name": name,
        "type": record_type,
        "exists": current is not None,
    }
    if current is not None:
        row = record_set_detail(current)
        card.update(
            {
                key: row[key]
                for key in ("fqdn", "ttl", "etag", "values", "alias_target_id", "metadata")
            }
        )
    return card


async def delete_card(ctx: PreviewContext) -> dict[str, Any] | None:
    """The card of a delete: the record set as it is now, and that it cannot be undone.

    Refuses (raises) when the record set is not the version the call names, so
    a person never approves values that are no longer there.
    """
    connector = _live_connector(ctx)
    if connector is None:
        return None
    path, group, zone, record_type, name = _checked_for_delete(
        resolve_target_config(ctx.target).subscription_id, ctx.params
    )
    etag = _etag_param(ctx.params)
    current = await find_record_set(connector, ctx.operator, ctx.target, path)
    if current is not None and etag is not None and current.get("etag") != etag:
        raise _changed(ctx.target, etag, current.get("etag"))
    return {
        "blast_radius": {
            "object": _card(record_type, group, zone, name, current),
            "children": [],
            "irreversibility": IRREVERSIBLE if current is not None else ALREADY_ABSENT,
        }
    }


async def upsert_card(ctx: PreviewContext) -> dict[str, Any] | None:
    """The card of a write: the record set as it is now next to the one to be written."""
    connector = _live_connector(ctx)
    if connector is None:
        return None
    path, group, zone, record_type, name = record_set_path(
        resolve_target_config(ctx.target).subscription_id,
        ctx.params,
        allowed_types=WRITE_RECORD_TYPES,
    )
    body = record_set_body(ctx.params, record_type)
    current = await find_record_set(connector, ctx.operator, ctx.target, path)
    proposed = record_set_detail(
        {"name": name, "type": f"Microsoft.Network/dnsZones/{record_type}", **body}
    )
    etag = _etag_param(ctx.params)
    card: dict[str, Any] = {
        "resource": "azure_dns_record_set",
        "current": _card(record_type, group, zone, name, current),
        "proposed": {key: proposed[key] for key in ("ttl", "values")},
        "mode": "create only" if etag is None else "change the version with this etag",
        "replaces": (
            "A change replaces the whole record set: the TTL and the values above are "
            "written, and anything on the current record set that is not listed under "
            "proposed (for example metadata) is dropped."
        ),
    }
    if etag is not None and current is not None:
        card["etag_matches_current"] = current.get("etag") == etag
    return card
