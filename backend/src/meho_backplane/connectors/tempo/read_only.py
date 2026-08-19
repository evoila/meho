# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read-only enforcement gate for the Tempo connector (#2903).

Grafana Tempo's HTTP query API on ``:3200`` is read-by-nature: trace fetch
(``/api/traces``), TraceQL search (``/api/search``), tag enumeration
(``/api/v2/search/...``) and TraceQL metrics (``/api/metrics/...``) are all
GETs, and Tempo has *no* operator-facing write API -- ingest is an OTLP push
from collectors on the distributor, not a query-frontend surface. The
connector is read-only **by construction** (every handler issues a GET through
:meth:`~meho_backplane.connectors.adapters.http.HttpConnector._request_json`,
which rejects non-idempotent verbs), but the generic ``tempo.get`` passthrough
lets a caller name an arbitrary path. This module is the belt-and-suspenders
gate over that passthrough:

* **Method gate** -- only ``GET`` is permitted.
* **Path allowlist** -- the path must live under ``/api`` (or be the bare
  ``/api`` root). Every documented query endpoint is under ``/api``; the
  probe/fingerprint reads ``/ready`` (outside ``/api``) travel a different,
  ungated seam (``_unauth_get``), so the passthrough surface stays tight.

Unlike the loki gate, this one carries **no** ``push``/``delete`` segment
blocklist, and deliberately so: Tempo exposes no state-changing endpoint that
is both a ``GET`` and under ``/api``. Its only mutating endpoints -- ``/flush``
and ``/shutdown`` (lifecycle, ``POST``) -- live *outside* ``/api`` entirely, so
the path scope already excludes them; the tenant-overrides writes under
``/api/overrides`` are ``POST``/``PATCH``/``DELETE``, refused by the method
gate. Loki needs its extra blocklist because ``GET /loki/api/v1/delete`` (a
read that lists pending deletes) lives *inside* its read prefix; Tempo has no
analogous GET-reachable write surface, so a segment blocklist here would be
dead code.

The gate is a pure function raising :class:`TempoReadOnlyError` -- no I/O, no
upstream call -- so a rejected write never reaches the wire and the unit tests
can prove rejection without a mock server.
"""

from __future__ import annotations

__all__ = [
    "TEMPO_API_PREFIX",
    "TempoReadOnlyError",
    "assert_tempo_read_only",
]

#: The only wire surface the ``tempo.get`` passthrough will reach. Every
#: query endpoint (trace / search / tags / metrics / echo / buildinfo) lives
#: under this prefix; the tenant-free ``/ready`` probe rides the ungated
#: ``_unauth_get`` seam, not this gate.
TEMPO_API_PREFIX = "/api"


class TempoReadOnlyError(ValueError):
    """A requested method/path would leave Tempo's read surface.

    Raised by :func:`assert_tempo_read_only` for a non-GET method or a path
    outside ``/api``. Subclasses :class:`ValueError` so the dispatcher's
    ``connector_error`` branch renders the message verbatim.
    """


def _normalize_path(path: str) -> str:
    """Return *path* stripped of a query string and normalised to a leading ``/``."""
    without_query = path.split("?", 1)[0].strip()
    if not without_query.startswith("/"):
        without_query = "/" + without_query
    return without_query


def assert_tempo_read_only(method: str, path: str) -> None:
    """Raise :class:`TempoReadOnlyError` unless *method*/*path* is a safe read.

    A safe read is a ``GET`` whose path lives under ``/api`` (Tempo's query
    surface). Case-insensitive on the method. No segment blocklist is applied
    -- see the module docstring for why Tempo needs none.
    """
    if method.upper() != "GET":
        raise TempoReadOnlyError(
            f"tempo connector is read-only: method {method!r} is not permitted "
            "(only GET reaches the Tempo API)"
        )

    normalized = _normalize_path(path)
    if normalized != TEMPO_API_PREFIX and not normalized.startswith(f"{TEMPO_API_PREFIX}/"):
        raise TempoReadOnlyError(
            f"path {path!r} is outside the allowed Tempo read surface ({TEMPO_API_PREFIX}/...)"
        )
