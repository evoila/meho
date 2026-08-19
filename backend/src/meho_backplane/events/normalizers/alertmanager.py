# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Alertmanager webhook (v4) payload normaliser + alert-triage prompt (#2882).

Covers Prometheus Alertmanager, the Loki ruler, and anything
Alertmanager-compatible. The marquee use case: a firing alert -> an agent
triage run. The v4 webhook body is a stable, documented shape:

    {"version": "4", "status": "firing", "receiver": "...",
     "groupLabels": {...}, "commonLabels": {"severity": "critical", ...},
     "commonAnnotations": {"summary": "..."}, "externalURL": "...",
     "alerts": [{"status": "...", "labels": {...}, "annotations": {...},
                 "startsAt": "...", "endsAt": "...", "fingerprint": "..."}]}

The group ``status`` (``firing`` / ``resolved``) is the ``{type}`` token, and
``commonLabels`` / ``commonAnnotations`` become the top-level ``labels`` /
``annotations`` an ``event_filter`` matches on.

Reference: https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meho_backplane.events.normalizers.base import (
    NormalizedEvent,
    as_mapping,
    common_alertmanager_fields,
    json_block,
    sanitise_event_type,
)

__all__ = ["build_prompt", "normalize"]


def normalize(parsed_body: object) -> NormalizedEvent:
    """Reduce an Alertmanager v4 body to ``(status, canonical labels/annotations, raw)``."""
    body = as_mapping(parsed_body)
    if body is None:
        return NormalizedEvent(event_type="event", match_fields={}, raw=parsed_body)
    return NormalizedEvent(
        event_type=sanitise_event_type(body.get("status")),
        match_fields=common_alertmanager_fields(body),
        raw=parsed_body,
    )


def build_prompt(payload: Mapping[str, Any]) -> str:
    """Compose an alert-triage prompt body from the normalised envelope.

    Reads the lifted top-level fields (``status`` / ``alertname`` /
    ``labels`` / ``annotations`` / ``num_alerts``). The returned body is the
    caller's to wrap in the untrusted-text envelope -- every interpolated
    value here is attacker-influenced sender content.
    """
    status = payload.get("status", "unknown")
    alertname = payload.get("alertname", "(unnamed)")
    num_alerts = payload.get("num_alerts", 0)
    labels = payload.get("labels", {})
    annotations = payload.get("annotations", {})

    lines = [f"Alertmanager alert group '{alertname}' is {status} ({num_alerts} alert(s))."]
    severity = labels.get("severity") if isinstance(labels, dict) else None
    if severity:
        lines.append(f"Severity: {severity}")
    summary = annotations.get("summary") if isinstance(annotations, dict) else None
    if summary:
        lines.append(f"Summary: {summary}")
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
