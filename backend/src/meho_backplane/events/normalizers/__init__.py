# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""First-wave per-vendor payload normalisers + prompt synthesis (#2882).

Two seams plug into the inbound-event pipeline #2881 shipped:

* **Ingest time** -- :func:`normalize_event` selects a per-kind normaliser by
  the resolved :class:`~meho_backplane.event_source.schemas.EventSourceKind`
  and reduces the raw sender body to a
  :class:`~meho_backplane.events.normalizers.base.NormalizedEvent`
  (``{type}`` token + canonical top-level match fields + full body under
  ``raw``). The #2881 ingest service builds the outbox envelope from it.
* **Fire time** -- :func:`synthesize_event_prompt` turns a matched event
  (``event_kind`` + normalised ``payload``) into the user turn a subscribed
  input-less ``kind=event`` trigger fires with. The composed body is
  **always** wrapped in the untrusted-text envelope
  (:func:`~meho_backplane.untrusted_text.wrap_untrusted_text`) -- the inbound
  payload is untrusted external input, so the fired agent must see it as data
  to act on, never a directive channel (the #2878 matcher's discipline).

The five kinds are a closed set (``EventSourceKind``); every member has a
normaliser here, pinned by :func:`registered_kinds`. An unrecognised kind (a
future enum member added without a normaliser) falls back to the
``generic-json`` normaliser rather than raising on the ingest path -- a
thin-but-durable event beats a ``500``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from meho_backplane.event_source.schemas import EventSourceKind
from meho_backplane.events.normalizers import (
    alertmanager,
    generic_json,
    grafana,
    harbor,
    vcf_operations,
)
from meho_backplane.events.normalizers.base import NormalizedEvent, json_block
from meho_backplane.untrusted_text import wrap_untrusted_text

__all__ = [
    "NormalizedEvent",
    "normalize_event",
    "registered_kinds",
    "synthesize_event_prompt",
]


@dataclass(frozen=True)
class _Vendor:
    """A kind's paired ingest-time normaliser and fire-time prompt builder."""

    normalize: Callable[[object], NormalizedEvent]
    build_prompt: Callable[[Mapping[str, Any]], str]


_REGISTRY: dict[str, _Vendor] = {
    EventSourceKind.ALERTMANAGER.value: _Vendor(alertmanager.normalize, alertmanager.build_prompt),
    EventSourceKind.GRAFANA.value: _Vendor(grafana.normalize, grafana.build_prompt),
    EventSourceKind.VCF_OPERATIONS.value: _Vendor(
        vcf_operations.normalize, vcf_operations.build_prompt
    ),
    EventSourceKind.HARBOR.value: _Vendor(harbor.normalize, harbor.build_prompt),
    EventSourceKind.GENERIC_JSON.value: _Vendor(generic_json.normalize, generic_json.build_prompt),
}

#: The fallback for an unrecognised kind -- fail safe to a durable, if thin,
#: event rather than raising on the ingest path.
_FALLBACK: _Vendor = _REGISTRY[EventSourceKind.GENERIC_JSON.value]

#: Trusted lead-in on a synthesised prompt. It carries no sender content, so
#: it sits *outside* the untrusted envelope; the event body (interpolated with
#: untrusted values) is wrapped separately.
_PROMPT_PREAMBLE: str = (
    "A subscribed MEHO event matched this trigger's filter and started this "
    "run. The event that fired it is described below; decide what to do based "
    "on it.\n\n"
)


def registered_kinds() -> frozenset[str]:
    """Return the source kinds with a registered normaliser (drift-guard for tests)."""
    return frozenset(_REGISTRY)


def normalize_event(kind: str, parsed_body: object) -> NormalizedEvent:
    """Normalise *parsed_body* with the normaliser for *kind* (fallback: generic-json)."""
    return _REGISTRY.get(kind, _FALLBACK).normalize(parsed_body)


def synthesize_event_prompt(event_kind: str, payload: Mapping[str, Any]) -> str:
    """Return the untrusted-enveloped agent prompt for a matched event.

    A per-vendor body is built when *event_kind* is an ``external.{kind}.*``
    kind with a registered normaliser; every other event kind (the internal
    ``agent_run.completed`` producer, and anything unrecognised) gets the
    generic structured render. The body -- whichever path -- is always wrapped
    with :func:`wrap_untrusted_text` before it reaches the fired agent.
    """
    source_kind = _external_source_kind(event_kind)
    vendor = _REGISTRY.get(source_kind) if source_kind is not None else None
    if vendor is not None:
        body = vendor.build_prompt(payload)
    else:
        body = _generic_prompt_body(event_kind, payload)
    return _PROMPT_PREAMBLE + wrap_untrusted_text(body)


def _external_source_kind(event_kind: str) -> str | None:
    """Extract ``{kind}`` from an ``external.{kind}.{type}`` event kind, else ``None``.

    Kind values never contain a dot, so ``parts[1]`` is the whole kind even
    when ``{type}`` does (a Harbor CloudEvents ``type`` of
    ``harbor.artifact.pushed`` yields ``external.harbor.harbor.artifact.pushed``
    -> ``harbor``). A non-``external`` kind (the internal
    ``agent_run.completed`` producer) yields ``None``.
    """
    parts = event_kind.split(".")
    if len(parts) >= 3 and parts[0] == "external":
        return parts[1]
    return None


def _generic_prompt_body(event_kind: str, payload: Mapping[str, Any]) -> str:
    """The event-agnostic render: the kind + payload as stable JSON.

    Preserves the #2878 matcher's original synthesised-prompt shape for
    internal events and any unrecognised kind.
    """
    return json_block({"event_kind": event_kind, "payload": dict(payload)})
