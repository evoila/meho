# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Coarse ``/ready`` probe: each configured search backend is reachable (T6 #1555).

Registered with :mod:`meho_backplane.health` from the FastAPI lifespan
(:mod:`meho_backplane.main`), beside the Keycloak / Vault / DB / broadcast
probes. It answers the deploy-level question "are the doc-search backends
wired?" — a coarse liveness gate, not the per-collection round-trip
:meth:`~meho_backplane.docs_search.backends.base.SearchBackend.probe`
performs.

Coarse by design
----------------

The probe consults each registered adapter's
:meth:`~meho_backplane.docs_search.backends.base.SearchBackend.is_configured`
— a cheap, synchronous, credential-free check (e.g. "is ``corpus_url``
set?"). It deliberately does **not** issue a live round-trip to each
backend: ``/ready`` is polled by the kubelet on a tight interval and a
live probe would (a) need an operator JWT the readiness path has no
business minting and (b) hammer the external corpus on every poll. The
per-collection liveness round-trip is the explicit
``POST /api/v1/doc_collections/{key}/probe`` operator action instead.

Empty-registry note
-------------------

With no registered backends the probe reports ``ok=True`` with
``detail="no backends registered"`` — the doc-search add-on being absent
is not a backplane-unready signal (the four core probes gate readiness).
The shipped ``corpus-http`` adapter self-registers at import, so a deploy
that imported the docs package always has at least that one to report on.
"""

from __future__ import annotations

import structlog

from meho_backplane.docs_search.backends import all_backends
from meho_backplane.health import ProbeResult

__all__ = ["PROBE_NAME", "docs_backends_readiness_probe"]

#: The ``name`` this probe registers under (and the ``checks[].name`` it
#: surfaces in the ``/ready`` payload).
PROBE_NAME = "docs_backends"

_log = structlog.get_logger(__name__)


def docs_backends_readiness_probe() -> ProbeResult:
    """Report whether every configured search backend is reachable.

    Synchronous (no I/O — :meth:`is_configured` is a config read), so the
    health registry calls it inline. ``ok`` is the AND across every
    registered adapter's :meth:`is_configured`; ``detail`` names the
    unconfigured backend types so an operator's single ``GET /ready``
    answers *which* backend is unwired without a second query. An empty
    registry is ``ok=True`` (the docs add-on is optional; its absence does
    not fail the backplane's readiness gate).
    """
    backends = all_backends()
    if not backends:
        return ProbeResult(name=PROBE_NAME, ok=True, detail="no backends registered")

    unconfigured = sorted(
        backend_type for backend_type, impl in backends.items() if not impl.is_configured()
    )
    if unconfigured:
        # Coarse fail: at least one registered backend is not wired. Name
        # the offending types (backend type strings are deploy config, not
        # tenant data, so they are safe to surface on /ready).
        _log.warning("docs_backends_unconfigured", unconfigured=unconfigured)
        return ProbeResult(
            name=PROBE_NAME,
            ok=False,
            detail=f"unconfigured: {', '.join(unconfigured)}",
        )
    return ProbeResult(name=PROBE_NAME, ok=True, detail=f"{len(backends)} backend(s) configured")
