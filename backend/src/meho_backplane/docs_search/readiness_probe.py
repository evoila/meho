# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Coarse ``/ready`` probe over the *configured* search backends (T6 #1555).

Registered with :mod:`meho_backplane.health` from the FastAPI lifespan
(:mod:`meho_backplane.main`), beside the Keycloak / Vault / DB / broadcast
probes. It answers the deploy-level question "which doc-search backends
are wired?" — a coarse config read, not the per-collection round-trip
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

Registered ≠ configured (#1606)
-------------------------------

The shipped ``corpus-http`` adapter self-registers at import, so every
deploy that imported the docs package has it in the registry — including
deploys that never set ``CORPUS_URL`` because the optional docs add-on is
simply not in use. An **unconfigured backend is therefore skipped, never
failed**: the docs add-on's absence is not a backplane-unready signal
(the four core probes gate readiness), and gating ``/ready`` on it
bricked a real deploy (503 forever, ``helm --wait`` timeout). The probe
reports ``ok=True`` unconditionally and is observability-only: ``detail``
says how many backends are configured so an operator's single
``GET /ready`` answers "is the docs add-on wired?".

Screen note (#3573)
-------------------

A set ``CORPUS_URL`` that the corpus destination screen would reject
(wrong scheme, or a private host not on ``MEHO_TARGET_SSRF_ALLOWLIST``)
still leaves ``ok=True``, but ``detail`` says so. Without the note,
``/ready`` looks green while every ``search_docs`` call fails with 503.
The check is offline: it parses the URL, checks the scheme against the
allowlist, and screens the host only when it is an IP literal. It never
does a DNS lookup, never contacts the corpus, and needs no credential,
so a slow DNS server cannot slow down ``/ready``. A hostname under
``https`` is not checked here; the dial and
``meho docs collections probe`` still catch a name that resolves to a
blocked address.

Fail-closed is preserved where the backend actually dials: an
unconfigured or unreachable corpus raises
:class:`~meho_backplane.auth.corpus.CorpusUnavailable` at call time
(``search_docs`` → HTTP 503) and fails the explicit per-collection probe
route — readiness of the *collection*, not of the backplane.
"""

from __future__ import annotations

import ipaddress

from meho_backplane.auth.corpus import corpus_endpoint_host
from meho_backplane.docs_search.backends import all_backends
from meho_backplane.health import ProbeResult
from meho_backplane.settings import get_settings
from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    assert_public_destination,
)

__all__ = ["PROBE_NAME", "docs_backends_readiness_probe"]

#: The ``name`` this probe registers under (and the ``checks[].name`` it
#: surfaces in the ``/ready`` payload).
PROBE_NAME = "docs_backends"


def docs_backends_readiness_probe() -> ProbeResult:
    """Report which search backends are configured — never failing readiness.

    Synchronous and free of I/O: :meth:`is_configured` is a config read,
    and the ``CORPUS_URL`` screen note is an offline check (no DNS lookup,
    see the module docstring). The async health sweep
    (:func:`~meho_backplane.health.run_probes_async`) runs sync probes on
    a worker thread; this one returns immediately either way. Registered ≠
    configured (#1606): the
    registry holds every backend that self-registered at import, while
    only the subset whose :meth:`is_configured` is true is actually wired
    on this deploy. Unconfigured backends are **skipped** — the docs
    add-on is optional, so ``ok`` is always ``True`` and ``detail``
    carries the configured count (or the no-backends note), plus a note
    when ``CORPUS_URL`` would be rejected by the destination screen. A configured
    backend's actual reachability is asserted where the call dials: the
    search path and the per-collection probe route fail closed with
    :class:`~meho_backplane.auth.corpus.CorpusUnavailable`.
    """
    backends = all_backends()
    if not backends:
        return ProbeResult(name=PROBE_NAME, ok=True, detail="no backends registered")

    configured = [impl for impl in backends.values() if impl.is_configured()]
    if not configured:
        return ProbeResult(name=PROBE_NAME, ok=True, detail="no backends configured")
    detail = f"{len(configured)} backend(s) configured"
    if not _corpus_url_passes_screen():
        detail += (
            "; CORPUS_URL is rejected by the corpus destination screen (use https"
            f" and a public host, or name the host on {TARGET_SSRF_ALLOWLIST_ENV})"
        )
    return ProbeResult(name=PROBE_NAME, ok=True, detail=detail)


def _corpus_url_passes_screen() -> bool:
    """Whether the set ``CORPUS_URL`` would pass the corpus destination screen.

    Offline only: checks the scheme and host against the allowlist, and
    screens the host only when it is an IP literal (no DNS lookup for a
    literal). A hostname is not resolved, so it passes here; the dial
    still screens it. An empty ``CORPUS_URL`` passes: there is nothing to
    dial. A malformed allowlist (``ValueError``) counts as a rejection,
    because the dial would fail on it too.
    """
    url = get_settings().corpus_url
    if not url:
        return True
    try:
        host = corpus_endpoint_host(url)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
        assert_public_destination(host)
    except ValueError:
        return False
    return True
