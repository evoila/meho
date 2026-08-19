# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-source ingest configuration resolved from ``event_source.extras`` (#2881).

The inbound ingest endpoint needs a handful of knobs per source -- the body
cap, the rate limit, the replay window, and the header names each auth
strategy reads. #2880 deliberately did not add dedicated columns for these:
it shipped ``event_source.extras`` (a JSON object, ``NOT NULL DEFAULT '{}'``)
as the escape hatch for exactly this kind of per-source tuning. This module
reads those keys off ``extras`` with safe defaults and defensive coercion,
so a malformed operator-authored value degrades to the default rather than
crashing the ingest path.

Body-cap ceiling
================

``max_body_bytes`` defaults to :data:`DEFAULT_MAX_BODY_BYTES` (256 KiB, the
in-repo pre-parse-cap precedent -- ``ui/csrf.py``, the connector-import and
topology caps). A per-source override can only *lower* it: the resolved cap
is ``min(override, DEFAULT_MAX_BODY_BYTES)`` so no operator config can widen
the endpoint's own DoS ceiling above 256 KiB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_DELIVERY_ID_HEADER",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_REPLAY_WINDOW_SECONDS",
    "DEFAULT_SIGNATURE_HEADER",
    "DEFAULT_STATIC_HEADER",
    "DEFAULT_TIMESTAMP_HEADER",
    "MAX_REPLAY_WINDOW_SECONDS",
    "IngestConfig",
    "resolve_ingest_config",
]

#: Pre-parse body cap (256 KiB). Matches the ``ui/csrf.py`` DoS-lesson cap
#: and the connector-import / topology caps. Also the hard ceiling a
#: per-source override may only lower, never raise.
DEFAULT_MAX_BODY_BYTES: int = 256 * 1024

#: Default replay tolerance for timestamp-bearing signatures, and the hard
#: upper bound a per-source override is clamped to (a wider window weakens
#: replay protection, so the endpoint caps how far an operator can open it).
DEFAULT_REPLAY_WINDOW_SECONDS: int = 300
MAX_REPLAY_WINDOW_SECONDS: int = 3600

#: Generic-sender header defaults. A vendor whose header names differ
#: (Grafana's ``X-Grafana-Alerting-Signature``) overrides these per-source
#: via ``extras`` -- keeping the verification code generic and the
#: vendor-specificity in config, not branches (#2882 owns richer vendor
#: normalisation).
DEFAULT_SIGNATURE_HEADER: str = "X-Meho-Signature"
DEFAULT_TIMESTAMP_HEADER: str = "X-Meho-Timestamp"
DEFAULT_STATIC_HEADER: str = "Authorization"
DEFAULT_DELIVERY_ID_HEADER: str = "X-Meho-Delivery-Id"


@dataclass(frozen=True)
class IngestConfig:
    """Resolved per-source ingest knobs. Frozen -- built once per request."""

    max_body_bytes: int
    replay_window_seconds: int
    require_timestamp: bool
    signature_header: str
    timestamp_header: str
    static_header: str
    delivery_id_header: str
    basic_username: str
    rate_per_minute_override: int | None


def _coerce_positive_int(value: Any, default: int, *, maximum: int | None = None) -> int:
    """Coerce *value* to a positive ``int``, falling back to *default*.

    A malformed operator-authored ``extras`` value (a string, a float, a
    negative) degrades to *default* rather than raising on the ingest path.
    When *maximum* is given the result is clamped so an override can only
    tighten a security-relevant bound, never widen it past the ceiling.
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    if coerced <= 0:
        return default
    if maximum is not None:
        return min(coerced, maximum)
    return coerced


def _coerce_str(value: Any, default: str) -> str:
    """Return *value* as a non-empty stripped ``str`` or *default*."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _coerce_rate_override(value: Any) -> int | None:
    """Resolve the per-source rate override: ``None`` (use the global) or ``>=0``.

    ``0`` is meaningful here (disable the limit for this one source), so this
    accepts the whole non-negative range rather than reusing the
    positive-only coercion. ``bool`` is a subclass of ``int`` but is never a
    valid cap, so it is rejected to ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def resolve_ingest_config(extras: dict[str, Any]) -> IngestConfig:
    """Build the :class:`IngestConfig` for a source from its ``extras`` dict.

    Every key is optional; an absent or malformed value yields the default.
    ``max_body_bytes`` is clamped to :data:`DEFAULT_MAX_BODY_BYTES` (the cap
    can only be lowered) and ``replay_window_seconds`` to
    :data:`MAX_REPLAY_WINDOW_SECONDS`.
    """
    return IngestConfig(
        max_body_bytes=_coerce_positive_int(
            extras.get("max_body_bytes"),
            DEFAULT_MAX_BODY_BYTES,
            maximum=DEFAULT_MAX_BODY_BYTES,
        ),
        replay_window_seconds=_coerce_positive_int(
            extras.get("replay_window_seconds"),
            DEFAULT_REPLAY_WINDOW_SECONDS,
            maximum=MAX_REPLAY_WINDOW_SECONDS,
        ),
        require_timestamp=bool(extras.get("require_timestamp", True)),
        signature_header=_coerce_str(extras.get("signature_header"), DEFAULT_SIGNATURE_HEADER),
        timestamp_header=_coerce_str(extras.get("timestamp_header"), DEFAULT_TIMESTAMP_HEADER),
        static_header=_coerce_str(extras.get("static_header"), DEFAULT_STATIC_HEADER),
        delivery_id_header=_coerce_str(
            extras.get("delivery_id_header"), DEFAULT_DELIVERY_ID_HEADER
        ),
        basic_username=_coerce_str(extras.get("basic_username"), ""),
        rate_per_minute_override=_coerce_rate_override(extras.get("rate_per_minute")),
    )
