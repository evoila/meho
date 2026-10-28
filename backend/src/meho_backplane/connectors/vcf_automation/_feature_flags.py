# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers for the VCFA provider feature-flag ops, plus their park-time preview.

* ``vcfa.provider.feature_flag.list`` -- ``GET /cloudapi/1.0.0/featureFlags``.
* ``vcfa.provider.feature_flag.set`` -- find the flag by name, ``GET`` it for
  its current state and ``ETag``, then ``PUT`` it with ``If-Match`` only when
  the state must change (``changed``); a flag already in the requested state
  answers ``unchanged`` and nothing is written.

A VCF Automation 9.1 appliance refuses a VM Apps (classic tenant) org while
the ``CLASSIC_TENANT_CREATION`` flag is off.
:func:`classic_tenant_creation_problem` lets ``vcfa.provider.org.create``
say so before any write, instead of passing on the upstream HTTP 400.

Wire facts follow ``go-vcloud-director`` v3 ``govcd/feture_flag.go`` and the
``FeatureFlag`` type in ``types/v56/tm.go`` (``id``, ``name``, ``usage``,
``enabled``, ``displayName``, ``displayDescription``); flag ids have the form
``urn:vcloud:featureflag:<NAME>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._lookups import list_all, quote_segment
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_FEATURE_FLAG_PATH,
    PROVIDER_FEATURE_FLAGS_PATH,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "CLASSIC_TENANT_FLAG",
    "classic_tenant_creation_problem",
    "provider_feature_flag_list",
    "provider_feature_flag_set",
]

#: Prefix of every feature-flag id.
_FLAG_URN_PREFIX: Final = "urn:vcloud:featureflag:"
#: The flag a VM Apps (classic tenant) org create needs on 9.1.
CLASSIC_TENANT_FLAG: Final = "CLASSIC_TENANT_CREATION"


def _flag_view(flag: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": flag.get("id"),
        "name": flag.get("name"),
        "displayName": flag.get("displayName"),
        "enabled": flag.get("enabled"),
    }


def _matches(flag: Mapping[str, Any], wanted: str) -> bool:
    """True when *wanted* is the flag's id, name, display name or id suffix."""
    needle = wanted.casefold()
    flag_id = str(flag.get("id") or "")
    short_id = flag_id.removeprefix(_FLAG_URN_PREFIX)
    keys = (flag_id, short_id, flag.get("name"), flag.get("displayName"))
    return any(isinstance(key, str) and key.casefold() == needle for key in keys if key)


async def _find_flag(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    wanted: str,
) -> dict[str, Any] | None:
    flags = await list_all(connector, target, operator, PROVIDER_FEATURE_FLAGS_PATH)
    return next((flag for flag in flags if _matches(flag, wanted)), None)


async def _read_flag(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    flag_id: str,
) -> tuple[dict[str, Any], str | None]:
    """``GET`` one flag by id → ``(flag, etag)``; the ``ETag`` may be absent."""
    resp = await connector._send_with_retry(
        target,
        "GET",
        PROVIDER_FEATURE_FLAG_PATH.format(id=quote_segment(flag_id)),
        operator=operator,
    )
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"vcf-automation feature flag {flag_id!r} is not a JSON object")
    return payload, resp.headers.get("ETag")


async def provider_feature_flag_list(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.feature_flag.list`` — ``GET /cloudapi/1.0.0/featureFlags``."""
    query = {key: params[key] for key in ("page", "pageSize") if params.get(key) is not None}
    return await connector._request_json(
        target, "GET", PROVIDER_FEATURE_FLAGS_PATH, operator=operator, params=query or None
    )


async def provider_feature_flag_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.feature_flag.set`` — turn one flag on or off (safe to run twice).

    Finds the flag by name before any write (``invalid_request`` on a miss),
    reads it by id for the current state and ``ETag``, and sends ``PUT`` with
    ``If-Match`` only when the state differs. The ``PUT`` body is the flag as
    read, with ``enabled`` replaced.
    """
    wanted, enabled = params["name"], bool(params["enabled"])
    found = await _find_flag(connector, target, operator, wanted)
    if found is None or not found.get("id"):
        return {
            "status": "invalid_request",
            "feature_flag": None,
            "previous_enabled": None,
            "guidance": (
                f"no feature flag named {wanted!r} on this appliance; nothing was written. "
                "List the flags with vcfa.provider.feature_flag.list"
            ),
        }
    flag_id = str(found["id"])
    current, etag = await _read_flag(connector, target, operator, flag_id)
    previous = bool(current.get("enabled"))
    if previous == enabled:
        return {
            "status": "unchanged",
            "feature_flag": _flag_view(current),
            "previous_enabled": previous,
            "guidance": (
                f"feature flag {flag_id!r} is already {'on' if enabled else 'off'}; "
                "nothing was written"
            ),
        }
    updated = await connector._post_json(
        target,
        PROVIDER_FEATURE_FLAG_PATH.format(id=quote_segment(flag_id)),
        operator=operator,
        verb="PUT",
        json={**current, "enabled": enabled},
        extra_headers={"If-Match": etag} if etag else None,
    )
    return {
        "status": "changed",
        "feature_flag": _flag_view(updated or {**current, "enabled": enabled}),
        "previous_enabled": previous,
        "guidance": None,
    }


async def classic_tenant_creation_problem(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
) -> str | None:
    """Why a VM Apps org create would be refused, or ``None`` when it may go ahead.

    Reads ``CLASSIC_TENANT_CREATION`` by id. Only a flag that is there and
    explicitly off blocks the create. A flag the appliance does not have, or
    a read that fails, lets the create go ahead -- the appliance then answers
    for itself.
    """
    try:
        flag, _etag = await _read_flag(
            connector, target, operator, _FLAG_URN_PREFIX + CLASSIC_TENANT_FLAG
        )
    except (httpx.HTTPStatusError, RuntimeError, ValueError):
        return None
    if flag.get("enabled") is not False:
        return None
    return (
        f"this appliance does not allow a VM Apps (classic tenant) org while the feature "
        f"flag {CLASSIC_TENANT_FLAG} ('Classic Tenant Creation') is off; nothing was written. "
        f"Turn it on with vcfa.provider.feature_flag.set (name '{CLASSIC_TENANT_FLAG}', "
        "enabled true), then run this op again"
    )


async def _feature_flag_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Park-time preview: the flag, its current state and the requested state.

    Without a connector (the egress-free ``preview_operation`` path) the
    current state is not read and ``would`` is ``"set_if_different"``. A
    failed read degrades to ``current_enabled: null`` rather than dropping
    the preview.
    """
    p = ctx.params
    wanted, requested = p.get("name"), p.get("enabled")
    if not isinstance(wanted, str) or not isinstance(requested, bool):
        return None
    preview: dict[str, Any] = {
        "action": "set_feature_flag",
        "name": wanted,
        "flag_id": None,
        "display_name": None,
        "current_enabled": None,
        "requested_enabled": requested,
        "would": "set_if_different",
    }
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    try:
        found = await _find_flag(connector, ctx.target, ctx.operator, wanted)
    except (httpx.HTTPError, RuntimeError, ValueError):
        return preview
    if found is None:
        return {**preview, "would": "invalid_request"}
    current = found.get("enabled")
    return {
        **preview,
        "flag_id": found.get("id"),
        "display_name": found.get("displayName"),
        "current_enabled": current,
        "would": "unchanged" if current == requested else "change",
    }


register_preview_builder("vcfa.provider.feature_flag.set", _feature_flag_set_preview)
