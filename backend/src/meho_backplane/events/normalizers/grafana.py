# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Grafana Alerting (>= 12.0) webhook payload normaliser + triage prompt (#2882).

Grafana's outgoing webhook body is Alertmanager-compatible -- the same
``status`` / ``commonLabels`` / ``commonAnnotations`` / ``alerts[]`` core --
plus a handful of Grafana-only top-level fields: ``state`` (``alerting`` /
``ok``), operator-templated ``title`` / ``message``, and ``orgId``. So the
normaliser reuses the shared Alertmanager extraction and adds ``state`` /
``title`` on top.

Grafana is also the strongest sender-auth story of the first wave: HMAC-SHA256
in an ``X-Grafana-Alerting-Signature`` header, signing ``timestamp + ":" +
body`` when a timestamp header is set. That is handled entirely by the #2881
auth layer via per-source ``extras`` header overrides -- no normaliser branch.

Reference:
https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meho_backplane.events.normalizers.base import (
    NormalizedEvent,
    as_mapping,
    as_text,
    common_alertmanager_fields,
    json_block,
    prune_none,
    sanitise_event_type,
)

__all__ = ["build_prompt", "normalize"]


def normalize(parsed_body: object) -> NormalizedEvent:
    """Reduce a Grafana Alerting body: Alertmanager core + ``state`` / ``title``."""
    body = as_mapping(parsed_body)
    if body is None:
        return NormalizedEvent(event_type="event", match_fields={}, raw=parsed_body)
    fields = common_alertmanager_fields(body)
    fields.update(
        prune_none(
            {
                "state": as_text(body.get("state")),
                "title": as_text(body.get("title")),
            }
        )
    )
    return NormalizedEvent(
        event_type=sanitise_event_type(body.get("status")),
        match_fields=fields,
        raw=parsed_body,
    )


def build_prompt(payload: Mapping[str, Any]) -> str:
    """Compose a Grafana alert-triage prompt body from the normalised envelope."""
    status = payload.get("status", "unknown")
    title = payload.get("title") or payload.get("alertname", "(unnamed)")
    num_alerts = payload.get("num_alerts", 0)
    labels = payload.get("labels", {})
    annotations = payload.get("annotations", {})

    lines = [f"Grafana alert '{title}' is {status} ({num_alerts} alert(s))."]
    severity = labels.get("severity") if isinstance(labels, dict) else None
    if severity:
        lines.append(f"Severity: {severity}")
    lines += [
        "",
        "Labels:",
        json_block(labels),
        "Annotations:",
        json_block(annotations),
        "",
        "Triage this alert and decide what action, if any, MEHO should take.",
    ]
    return "\n".join(lines)
