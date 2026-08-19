# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Harbor per-project webhook payload normaliser + automation prompt (#2882).

Harbor is the first-wave's non-alert automation family: artifact push/pull,
scan-complete, quota, replication, and tag-retention events off per-project
webhook policies. Two payload shapes, selectable per policy:

* **Default JSON** -- the native shape::

      {"type": "PUSH_ARTIFACT", "occur_at": 1586922308, "operator": "admin",
       "event_data": {"resources": [{"digest": "...", "tag": "latest",
                                     "resource_url": "..."}],
                      "repository": {"name": "nginx", "namespace": "library",
                                     "repo_full_name": "library/nginx",
                                     "repo_type": "public"}}}

* **CloudEvents** -- the same object wrapped in a CloudEvents 1.0 envelope
  (``specversion`` / ``id`` / ``source`` / ``type`` / ``time`` / ``data``).
  We unwrap ``data`` and normalise the inner default-shape object, so a policy
  emitting either format yields the same ``event_type`` and match fields.

The ``type`` (e.g. ``PUSH_ARTIFACT``) is the ``{type}`` token; ``operator``
and the ``repository`` (plus its ``namespace``) are lifted for filtering.

Reference:
https://goharbor.io/docs/latest/working-with-projects/project-configuration/configure-webhooks/
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

__all__ = ["build_prompt", "normalize"]


def _unwrap_cloudevents(body: dict[str, Any]) -> dict[str, Any]:
    """Return the inner Harbor object from a CloudEvents envelope, else *body* itself.

    A CloudEvents body carries ``specversion`` and nests the Harbor
    default-shape object under ``data``; unwrapping it lets one code path
    serve both formats.
    """
    if as_text(body.get("specversion")) is not None:
        inner = as_mapping(body.get("data"))
        if inner is not None:
            return inner
    return body


def normalize(parsed_body: object) -> NormalizedEvent:
    """Reduce a Harbor webhook body (default or CloudEvents) to the canonical shape."""
    body = as_mapping(parsed_body)
    if body is None:
        return NormalizedEvent(event_type="event", match_fields={}, raw=parsed_body)
    inner = _unwrap_cloudevents(body)
    event_data = as_mapping(inner.get("event_data"))
    repository = as_mapping(event_data.get("repository")) if event_data is not None else None
    fields = prune_none(
        {
            "type": as_text(inner.get("type")),
            "operator": as_text(inner.get("operator")),
            "repository": repository,
            "namespace": as_text(repository.get("namespace")) if repository is not None else None,
        }
    )
    return NormalizedEvent(
        event_type=sanitise_event_type(inner.get("type")),
        match_fields=fields,
        raw=parsed_body,
    )


def build_prompt(payload: Mapping[str, Any]) -> str:
    """Compose a Harbor automation prompt body from the normalised envelope."""
    event_type = payload.get("type", payload.get("event_type", "event"))
    operator = payload.get("operator")
    repository = payload.get("repository")

    headline = f"Harbor webhook event '{event_type}'"
    if operator:
        headline += f" by operator '{operator}'"
    lines = [headline + "."]
    if isinstance(repository, dict):
        repo_name = repository.get("repo_full_name") or repository.get("name")
        if repo_name:
            lines.append(f"Repository: {repo_name}")
    lines += [
        "",
        "Event data:",
        json_block(payload.get("raw")),
        "",
        "Handle this registry event and decide what automation MEHO should run.",
    ]
    return "\n".join(lines)
