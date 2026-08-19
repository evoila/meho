# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Generic-JSON payload normaliser + prompt (#2882).

The catch-all for templated senders that speak MEHO's own signing scheme
(``X-Meho-Signature`` HMAC-SHA256 + timestamp): ArgoCD notifications, Proxmox
VE 8.3+ webhook targets, the p2-inc Keycloak events SPI, and custom scripts.

There is no vendor schema to curate, so the normaliser lifts **every**
top-level key of a JSON-object body as a match field -- the operator filters
directly on their own payload shape (``{"severity": "high"}``) with no ``raw.``
prefix. A ``type`` hint, when present, becomes the ``{type}`` token. A body
that is a top-level array or scalar carries no top-level fields to lift, so it
normalises to an empty match set with the body under ``raw``.

The full body is always preserved under ``raw`` as well, so a collision
between an operator key and a reserved envelope key (``source`` /
``event_type`` / ``received_at`` / ``raw``) is still filterable there.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meho_backplane.events.normalizers.base import (
    NormalizedEvent,
    as_mapping,
    json_block,
    sanitise_event_type,
)

__all__ = ["build_prompt", "normalize"]


def normalize(parsed_body: object) -> NormalizedEvent:
    """Lift every top-level key as a match field; derive ``{type}`` from ``type``."""
    body = as_mapping(parsed_body)
    if body is None:
        return NormalizedEvent(event_type="event", match_fields={}, raw=parsed_body)
    return NormalizedEvent(
        event_type=sanitise_event_type(body.get("type")),
        match_fields=dict(body),
        raw=parsed_body,
    )


def build_prompt(payload: Mapping[str, Any]) -> str:
    """Compose a generic external-event prompt body from the raw sender payload."""
    event_type = payload.get("event_type", "event")
    return (
        f"External event of type '{event_type}' from a generic-json source.\n\n"
        + json_block(payload.get("raw"))
        + "\n\nDecide what action, if any, MEHO should take based on this event."
    )
