# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCF Operations outbound-webhook payload normaliser + triage prompt (#2882).

VCF Operations (formerly Aria Operations / vRealize Operations) is the
aggregation point for the whole VMware estate -- vCenter, NSX, and vSAN
alerts all surface as VCF Operations alerts -- and its outbound Webhook
notification plugin is the **only** viable vSphere push path in 2026:
vCenter has no native webhook and VEBA was archived read-only in 2025. So we
normalise VCF Operations, never vCenter directly.

The catch: the Webhook plugin's body is **entirely operator-templated** (the
"Payload of the request" box, built from ``${...}`` parameters), so there is
no vendor-fixed schema to parse. MEHO therefore ships a **recommended payload
template** (documented in ``docs/codebase/events.md``) that emits a stable set
of top-level keys, and this normaliser lifts exactly those. An operator who
keeps a custom template still ingests fine -- their fields are absent from the
match set but preserved verbatim under ``raw`` for filtering as
``{"raw": {...}}``.

Recommended-template keys (all optional, all string):
``alert_name``, ``status``, ``criticality``, ``alert_type``, ``sub_type``,
``resource_name``, ``resource_kind``, ``adapter_kind``, ``alert_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meho_backplane.events.normalizers.base import (
    NormalizedEvent,
    as_mapping,
    as_text,
    json_block,
    prune_none,
    sanitise_event_type,
)

__all__ = ["MATCH_KEYS", "build_prompt", "normalize"]

#: Top-level keys MEHO's recommended VCF Operations payload template emits and
#: this normaliser lifts as canonical match fields. ``status`` (the alert
#: state, e.g. ``active`` / ``canceled``) is also the ``{type}`` token.
MATCH_KEYS: tuple[str, ...] = (
    "alert_name",
    "status",
    "criticality",
    "alert_type",
    "sub_type",
    "resource_name",
    "resource_kind",
    "adapter_kind",
    "alert_id",
)


def normalize(parsed_body: object) -> NormalizedEvent:
    """Lift the recommended-template keys; everything else stays under ``raw``."""
    body = as_mapping(parsed_body)
    if body is None:
        return NormalizedEvent(event_type="event", match_fields={}, raw=parsed_body)
    return NormalizedEvent(
        event_type=sanitise_event_type(body.get("status")),
        match_fields=prune_none({key: as_text(body.get(key)) for key in MATCH_KEYS}),
        raw=parsed_body,
    )


def build_prompt(payload: Mapping[str, Any]) -> str:
    """Compose a VCF Operations alert-triage prompt body from the envelope."""
    alert_name = payload.get("alert_name", "(unnamed alert)")
    status = payload.get("status", "unknown")
    criticality = payload.get("criticality")
    resource_name = payload.get("resource_name")
    resource_kind = payload.get("resource_kind")

    headline = f"VCF Operations alert '{alert_name}' is {status}"
    if criticality:
        headline += f" (criticality: {criticality})"
    lines = [headline + "."]
    if resource_name:
        target = resource_name
        if resource_kind:
            target += f" [{resource_kind}]"
        lines.append(f"Affected object: {target}")
    lines += [
        "",
        "Alert fields:",
        json_block({key: payload[key] for key in MATCH_KEYS if key in payload}),
        "",
        "Triage this alert across the VMware estate and decide what MEHO should do.",
    ]
    return "\n".join(lines)
