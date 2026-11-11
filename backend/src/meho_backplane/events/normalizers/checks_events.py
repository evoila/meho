# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Prompt for an input-less event trigger fired by a ``check.*`` event (#6180).

The facts match the investigator briefing (``checks/investigate.py``,
``_build_briefing``): the Dashboard or Sensor, the change, and the Sensors of
the cause group. The caller wraps the result in the untrusted-text envelope,
as for every event.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meho_backplane.events.normalizers.base import as_mapping, as_text

__all__ = ["CHECK_EVENT_PREFIX", "build_prompt"]

CHECK_EVENT_PREFIX = "check."


def _text(value: object) -> str:
    return as_text(value) or "unknown"


def build_prompt(event_kind: str, payload: Mapping[str, Any]) -> str:
    """Render the facts of a ``check.sensor.changed`` or ``check.dashboard.changed`` event."""
    source = as_mapping(payload.get("source")) or {}
    kind = _text(source.get("kind"))
    lines = [
        f"event: {event_kind}",
        f"{kind}: {_text(source.get('name'))}",
        f"change: {_text(payload.get('previous_state'))} -> {_text(payload.get('state'))}",
        f"worsening: {'yes' if payload.get('worsening') else 'no'}",
    ]
    if kind == "sensor":
        lines.append(f"severity: {_text(payload.get('severity'))}")
        lines.append(f"target: {_text(payload.get('target'))}")
        lines.append(f"op: {_text(payload.get('connector_id'))} {_text(payload.get('op_id'))}")
    group = as_mapping(payload.get("cause_group"))
    if group is not None:
        lines.append(f"correlation_group: {_text(group.get('key'))}")
        lines.append("sensors in this cause group:")
        members = group.get("members")
        for member in members if isinstance(members, list) else []:
            item = as_mapping(member) or {}
            lines.append(
                f"- name: {_text(item.get('name'))}, state: {_text(item.get('state'))}, "
                f"target: {_text(item.get('target'))}"
            )
    if payload.get("known_noise"):
        lines.append("known noise: yes (a stored policy says this cause is benign)")
    return "\n".join(lines)
