# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared substrate for the first-wave payload normalisers (#2882).

Every per-kind normaliser turns a vendor's raw inbound webhook body into a
:class:`NormalizedEvent`: the ``{type}`` token for the
``external.{kind}.{type}`` event kind, the **canonical match fields** lifted
to the outbox payload's top level (so ``event_filter`` authoring stays simple
-- ``{"status": "firing", "labels": {"severity": "critical"}}``), and the
full sender body preserved verbatim under ``raw``.

The whole inbound body is **untrusted external input**, so every accessor
here is defensive: a wrong-typed / missing / partial field degrades to
``None`` (and is pruned from the match fields) rather than raising. A
top-level array or scalar body -- legal JSON that is not an object -- yields
an empty match-field set and the body under ``raw``, never a crash. This is
what lets the ingest path map a malformed/partial payload to a clean ``400``
(the parse boundary) or an accepted-but-thin event, never a ``500``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_EVENT_TYPE_LEN",
    "NormalizedEvent",
    "as_mapping",
    "as_text",
    "common_alertmanager_fields",
    "json_block",
    "prune_none",
    "sanitise_event_type",
]

#: Max length of the derived ``event_type`` token in ``external.{kind}.{type}``.
#: Bounds an attacker-influenced ``type`` hint from bloating the event kind.
MAX_EVENT_TYPE_LEN: int = 64


@dataclass(frozen=True)
class NormalizedEvent:
    """A vendor payload reduced to the shape the outbox + matcher consume.

    * ``event_type`` -- the sanitised ``{type}`` token (never empty; defaults
      to ``"event"``).
    * ``match_fields`` -- canonical fields the service lifts to the outbox
      payload's **top level** for ``payload @> event_filter`` matching. Never
      carries a ``None`` value (absent fields are pruned).
    * ``raw`` -- the full parsed sender body, verbatim, stored under the
      envelope's ``raw`` key. May be any JSON shape (object, array, scalar).
    """

    event_type: str
    match_fields: dict[str, Any]
    raw: Any


def sanitise_event_type(raw: object) -> str:
    """Reduce a payload ``type`` hint to a safe ``[a-z0-9._-]`` token.

    Untrusted external input, so a non-string / empty / all-stripped value
    degrades to ``"event"`` and anything outside the allowed set is dropped.
    (Moved verbatim from the #2881 ingest service so all normalisers share
    one derivation.)
    """
    if not isinstance(raw, str):
        return "event"
    kept = "".join(c for c in raw.lower() if c.isalnum() or c in "._-")
    return kept[:MAX_EVENT_TYPE_LEN] or "event"


def as_mapping(value: object) -> dict[str, Any] | None:
    """Return *value* as a shallow ``dict`` copy when it is a JSON object, else ``None``.

    The copy detaches the extracted match field from the ``raw`` body so the
    two envelope branches never alias the same nested object.
    """
    return dict(value) if isinstance(value, dict) else None


def as_text(value: object) -> str | None:
    """Return *value* when it is a non-empty string, else ``None``."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def json_block(value: Any) -> str:
    """Render *value* as stable, indented JSON for embedding inside a prompt body.

    ``default=str`` keeps a non-JSON scalar (a stray datetime) renderable
    rather than raising while composing the fired agent's prompt.
    """
    return json.dumps(value, sort_keys=True, indent=2, default=str)


def prune_none(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None`` so an absent field never lands in the envelope.

    Keeps the outbox payload (and every operator-authored ``event_filter``
    written against it) free of ``"field": null`` noise that a vendor payload
    missing that field would otherwise inject.
    """
    return {key: value for key, value in fields.items() if value is not None}


def common_alertmanager_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the Alertmanager-v4 canonical match fields (shared by AM + Grafana).

    Grafana Alerting emits an Alertmanager-compatible envelope, so both
    normalisers lift the same core fields: the group ``status``, the
    ``commonLabels`` (surfaced as ``labels`` -- the key one for a
    ``{"labels": {"severity": "critical"}}`` filter), the
    ``commonAnnotations`` (as ``annotations``), the resolved ``alertname``,
    the ``receiver``, and the alert count. Every value is defensively
    coerced and ``None`` values are pruned.
    """
    common_labels = as_mapping(body.get("commonLabels"))
    group_labels = as_mapping(body.get("groupLabels"))
    alertname = common_labels.get("alertname") if common_labels is not None else None
    if alertname is None and group_labels is not None:
        alertname = group_labels.get("alertname")
    alerts = body.get("alerts")
    return prune_none(
        {
            "status": as_text(body.get("status")),
            "labels": common_labels,
            "annotations": as_mapping(body.get("commonAnnotations")),
            "alertname": as_text(alertname),
            "receiver": as_text(body.get("receiver")),
            "num_alerts": len(alerts) if isinstance(alerts, list) else None,
        }
    )
