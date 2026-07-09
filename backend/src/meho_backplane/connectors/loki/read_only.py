# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only enforcement gate for the Loki connector (#2235).

Loki's HTTP API mixes read verbs (``query`` / ``query_range`` / ``labels`` /
``series``) with the ingest verb ``POST /loki/api/v1/push`` and the deletion
verbs on ``/loki/api/v1/delete`` (``POST`` / ``PUT`` create a delete request,
``DELETE`` cancels one, ``GET`` lists them). The connector is read-only **by
construction** — every handler issues a GET through
:meth:`~meho_backplane.connectors.adapters.http.HttpConnector._request_json`,
which rejects non-idempotent verbs — but the generic ``loki.get`` passthrough
lets a caller name an arbitrary path. This module is the belt-and-suspenders
gate over that passthrough:

* **Method gate** — only ``GET`` is permitted.
* **Path allowlist** — the path must live under ``/loki/api/v1``.
* **Write/delete blocklist** — any path segment equal to ``push`` or beginning
  ``delete`` is refused, so even ``GET /loki/api/v1/delete`` (the "list delete
  requests" read) is blocked. The blocklist is deliberately stricter than the
  method gate: it fails closed on the whole delete surface rather than trusting
  the verb alone.

The gate is a pure function raising :class:`LokiReadOnlyError` — no I/O,
no upstream call — so a rejected write never reaches the wire and the unit
tests can prove rejection without a mock server.
"""

from __future__ import annotations

__all__ = [
    "LOKI_API_V1_PREFIX",
    "LokiReadOnlyError",
    "assert_loki_read_only",
]

#: The only wire surface the connector will reach. Every op path and the
#: ``loki.get`` passthrough must live under this prefix (or be the bare
#: ``/loki/api/v1`` root).
LOKI_API_V1_PREFIX = "/loki/api/v1"


class LokiReadOnlyError(ValueError):
    """A requested method/path would mutate Loki state (or leaves the read surface).

    Raised by :func:`assert_loki_read_only` for a non-GET method, a path
    outside ``/loki/api/v1``, or a path whose segments target the ``push``
    ingest or ``delete`` surface. Subclasses :class:`ValueError` so the
    dispatcher's ``connector_error`` branch renders the message verbatim.
    """


def _normalize_path(path: str) -> str:
    """Return *path* stripped of a query string and normalised to a leading ``/``."""
    without_query = path.split("?", 1)[0].strip()
    if not without_query.startswith("/"):
        without_query = "/" + without_query
    return without_query


def assert_loki_read_only(method: str, path: str) -> None:
    """Raise :class:`LokiReadOnlyError` unless *method*/*path* is a safe read.

    A safe read is a ``GET`` whose path lives under ``/loki/api/v1`` and whose
    segments name neither the ``push`` ingest endpoint nor any ``delete*``
    endpoint. Case-insensitive on both the method and the blocked segments.
    """
    if method.upper() != "GET":
        raise LokiReadOnlyError(
            f"loki connector is read-only: method {method!r} is not permitted "
            "(only GET reaches the Loki API)"
        )

    normalized = _normalize_path(path)
    if normalized != LOKI_API_V1_PREFIX and not normalized.startswith(f"{LOKI_API_V1_PREFIX}/"):
        raise LokiReadOnlyError(
            f"path {path!r} is outside the allowed Loki read surface ({LOKI_API_V1_PREFIX}/...)"
        )

    # Segment-exact blocklist so a legitimate label value that merely *contains*
    # 'push' or 'delete' as a substring is not caught, while the actual
    # ``/push`` and ``/delete*`` endpoints (any verb) are refused.
    for segment in normalized.split("/"):
        lowered = segment.lower()
        if lowered == "push" or lowered.startswith("delete"):
            raise LokiReadOnlyError(
                f"path {path!r} targets the Loki write/delete surface "
                f"(segment {segment!r}); the connector is read-only"
            )
