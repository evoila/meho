# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression tests for the reverse-proxy header contract (Issue #730).

Background
----------

The backplane Pod runs uvicorn behind a TLS-terminating Ingress. By
default uvicorn populates the ASGI scope's ``scheme`` from the
on-the-wire scheme (``http`` after the Ingress terminates TLS), which
FastAPI's ``redirect_slashes`` reflects into the ``Location`` header
of trailing-slash 307 redirects:

::

    GET /api/v1/connectors/k8s-1.x/operations  (no trailing slash)
    → 307  Location: http://meho.evba.lab/api/v1/connectors/k8s-1.x/operations/
                     ^^^^ HTTPS→HTTP downgrade

That downgrade lets an on-path attacker observe (and tamper with) the
client's second hop. The fix has two halves:

1. Run uvicorn with ``--proxy-headers`` so it honours
   ``X-Forwarded-Proto`` / ``X-Forwarded-For``. The Dockerfile's CMD
   adds the flag; see ``backend/Dockerfile``.
2. Restrict the trusted-proxies list to known in-cluster proxies via
   the ``FORWARDED_ALLOW_IPS`` env var. The Helm chart wires this via
   ``config.forwardedAllowIps`` (default ``127.0.0.1``, operators
   pin to their Ingress controller's pod CIDR). See
   ``deploy/charts/meho/values.yaml`` and
   ``docs/cross-repo/reverse-proxy-contract.md``.

Test strategy
-------------

The middleware uvicorn installs when ``--proxy-headers`` is set lives
at the ASGI **server** layer, not inside the FastAPI app — wrapping
it around a minimal FastAPI sub-app that mirrors the contract (a
trailing-slash GET route with ``redirect_slashes=True``) is the
faithful integration: it exercises the exact same
``ProxyHeadersMiddleware`` constructor + scope-rewrite code path
uvicorn runs in-cluster, without dragging the chassis' settings /
DB / Vault dependencies into the test.

The production app's ``redirect_slashes`` setting is asserted
directly so a future ``FastAPI(redirect_slashes=False)`` would trip
this test loudly — that knob is the second half of the bug surface
(disabling the redirects removes the bug, but also removes the
canonical-trailing-slash UX the backplane relies on, which is a
behavioural change not in scope here).

A separate Dockerfile-source assertion guards the ``--proxy-headers``
flag against accidental removal.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def _make_redirect_app() -> FastAPI:
    """Minimal FastAPI app with a trailing-slash route.

    Mirrors the production contract — a router prefix + a route
    registered with the trailing slash — so the request without the
    trailing slash triggers FastAPI's ``redirect_slashes`` 307. The
    handler body is intentionally trivial; the bug surface is the
    redirect, not the response.

    FastAPI defaults ``redirect_slashes=True`` (the production app
    also relies on that default — see
    :func:`test_production_app_keeps_redirect_slashes_enabled`). We
    construct the minimal app with the explicit value pinned so this
    test's contract survives a future FastAPI default flip.
    """
    app = FastAPI(redirect_slashes=True)

    @app.get("/api/v1/widgets/")
    def list_widgets() -> dict[str, list[str]]:
        return {"widgets": []}

    return app


@pytest.fixture
def trusted_client() -> Iterator[TestClient]:
    """Driver for the trusted-upstream path.

    ``trusted_hosts='*'`` mirrors the operator-configured
    ``FORWARDED_ALLOW_IPS=*`` posture used by single-tenant labs /
    ephemeral CI clusters; production overrides pin to the Ingress
    controller's pod CIDR. The trust posture is documented in
    ``docs/cross-repo/reverse-proxy-contract.md``.
    """
    wrapped = ProxyHeadersMiddleware(_make_redirect_app(), trusted_hosts="*")
    yield TestClient(wrapped)


@pytest.fixture
def untrusted_client() -> Iterator[TestClient]:
    """Driver for the fail-closed loopback-only path.

    Mirrors the chart's default (``FORWARDED_ALLOW_IPS=127.0.0.1``).
    The TestClient's synthetic transport reports a non-loopback peer
    by default, so ``X-Forwarded-Proto`` sent through this client is
    expected to be **ignored**. This is the fail-closed leg of the
    trust contract: a misconfigured cluster's redirects keep
    downgrading rather than silently believing untrusted peers.
    """
    wrapped = ProxyHeadersMiddleware(_make_redirect_app(), trusted_hosts="127.0.0.1")
    yield TestClient(wrapped)


def test_trailing_slash_redirect_reflects_x_forwarded_proto_https(
    trusted_client: TestClient,
) -> None:
    """Trailing-slash 307 reflects ``X-Forwarded-Proto: https`` into ``Location``.

    Hits ``/api/v1/widgets`` without a trailing slash; the route is
    registered as ``/api/v1/widgets/`` so FastAPI emits a 307 to the
    slashed form. The ``Location`` header is what carries the
    regression: it must start with ``https://`` when the upstream
    proxy declares the original scheme via ``X-Forwarded-Proto`` and
    the connecting peer is in the trust list.
    """
    response = trusted_client.get(
        "/api/v1/widgets",
        headers={
            "X-Forwarded-Proto": "https",
            "Host": "meho.evba.lab",
        },
        follow_redirects=False,
    )

    assert response.status_code == 307, response.text
    location = response.headers["location"]
    assert location.startswith("https://"), (
        f"expected Location to reflect X-Forwarded-Proto=https, got {location!r} — "
        "the HTTPS→HTTP redirect downgrade (Issue #730) has regressed"
    )
    assert location == "https://meho.evba.lab/api/v1/widgets/"


def test_trailing_slash_redirect_without_forwarded_proto_uses_request_scheme(
    trusted_client: TestClient,
) -> None:
    """Without ``X-Forwarded-Proto``, the 307 reflects the request's scheme.

    The TestClient's synthetic transport uses ``http`` on the wire,
    so absent any forwarded-proto header the ``Location`` is
    ``http://``. This pins the contract — the redirect scheme tracks
    the (effective) request scheme exactly, and the proxy-header
    middleware does not invent ``https://`` out of thin air.
    """
    response = trusted_client.get(
        "/api/v1/widgets",
        headers={"Host": "meho.evba.lab"},
        follow_redirects=False,
    )

    assert response.status_code == 307, response.text
    assert response.headers["location"] == "http://meho.evba.lab/api/v1/widgets/"


def test_untrusted_upstream_ignores_x_forwarded_proto(
    untrusted_client: TestClient,
) -> None:
    """Loopback-only trust → ``X-Forwarded-Proto`` from a non-loopback peer is ignored.

    This is the security half of the contract. ``--proxy-headers``
    with ``FORWARDED_ALLOW_IPS=127.0.0.1`` (uvicorn's secure default,
    which the chart inherits when an operator forgets to override
    ``config.forwardedAllowIps``) **must** ignore ``X-Forwarded-Proto``
    from a peer outside the trust list. The fail-closed symptom (the
    redirect stays ``http://``) is visible in production curl
    diagnostics, and that's what makes the misconfiguration loud
    rather than silently trusted.

    The TestClient's synthetic transport reports its peer as
    ``testclient`` (resolved to a non-loopback address by Starlette);
    that suffices to drive the untrusted-peer code path.
    """
    response = untrusted_client.get(
        "/api/v1/widgets",
        headers={
            "X-Forwarded-Proto": "https",
            "Host": "meho.evba.lab",
        },
        follow_redirects=False,
    )

    assert response.status_code == 307, response.text
    location = response.headers["location"]
    assert location.startswith("http://"), (
        f"expected untrusted X-Forwarded-Proto to be ignored, got {location!r} — "
        "trust restriction (FORWARDED_ALLOW_IPS) is not being enforced"
    )


def test_production_app_keeps_redirect_slashes_enabled() -> None:
    """The production FastAPI app must keep ``redirect_slashes=True``.

    The bug surface this Initiative fixes is the **scheme** the
    redirect reflects, not the redirects themselves. Disabling
    ``redirect_slashes`` would remove the bug surface but also break
    the canonical-trailing-slash UX the backplane's routers rely on
    (e.g. ``/api/v1/connectors`` → ``/api/v1/connectors/``). Pin the
    setting so a future "let's just turn off redirects" commit fails
    this test and surfaces the tradeoff explicitly.

    Lazy import keeps the chassis settings load out of the
    test-collection path — Dockerfile / minimal-app tests above do
    not need a full chassis env.
    """
    import os

    # Provide the chassis env vars the production app's settings
    # layer reads at import time. Mirrors
    # ``tests/test_api_v1_health.py`` fixtures.
    for k, v in {
        "KEYCLOAK_ISSUER_URL": "https://keycloak.test/realms/meho",
        "KEYCLOAK_AUDIENCE": "meho-backplane",
        "KEYCLOAK_JWKS_CACHE_TTL_SECONDS": "300",
        "KEYCLOAK_JWT_LEEWAY_SECONDS": "30",
        "VAULT_ADDR": "https://vault.test",
        "VAULT_OIDC_ROLE": "meho-mcp",
        "VAULT_OIDC_MOUNT_PATH": "jwt",
        "VAULT_TIMEOUT_SECONDS": "5.0",
    }.items():
        os.environ.setdefault(k, v)

    from meho_backplane.main import app as production_app

    assert production_app.router.redirect_slashes is True, (
        "production app no longer trailing-slash-redirects; if this was "
        "intentional, the proxy-header fix (Issue #730) is moot but the UX "
        "regression needs its own justification"
    )


def test_dockerfile_cmd_contains_proxy_headers_flag() -> None:
    """The shipped Dockerfile's uvicorn CMD MUST include ``--proxy-headers``.

    A drift-tripwire on the Dockerfile: a future refactor that drops
    the flag (e.g. someone "simplifying" the CMD line) would silently
    re-introduce the HTTPS→HTTP downgrade in production while every
    other test in this file still passed (they exercise the
    middleware by direct wrap, not via the Dockerfile). Asserting
    the flag presence on the source-of-truth file catches that
    regression.

    Reads via the repo-relative path resolved from this test file's
    location — same pattern as ``tests/test_app_starts.py`` reading
    ``pyproject.toml``.
    """
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    # Looser substring match on the CMD line — the flag may move
    # within the JSON-array CMD across future refactors, but it must
    # be present in the file.
    assert "--proxy-headers" in content, (
        f"{dockerfile} no longer passes --proxy-headers to uvicorn; "
        "the HTTPS→HTTP redirect downgrade (Issue #730) will regress in production"
    )


def test_chart_configmap_renders_forwarded_allow_ips_env() -> None:
    """The chart's ConfigMap template MUST render ``FORWARDED_ALLOW_IPS``.

    Source-text gate on the chart side: if a future commit drops the
    line that maps ``config.forwardedAllowIps`` into the env var, the
    backplane Pod's uvicorn reverts to its compiled-in default
    (``127.0.0.1`` only), and operators who set the value in
    ``values.yaml`` will be silently ignored. Catching that with a
    runtime helm-template test would require helm to be installed in
    every pytest environment; a substring check on the template is
    cheap and CI-environment-agnostic.
    """
    configmap = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "charts"
        / "meho"
        / "templates"
        / "configmap.yaml"
    )
    content = configmap.read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS:" in content, (
        f"{configmap} no longer renders FORWARDED_ALLOW_IPS; "
        "the chart's forwardedAllowIps knob is unwired (Issue #730)"
    )
    assert ".Values.config.forwardedAllowIps" in content, (
        f"{configmap} renders FORWARDED_ALLOW_IPS but not from "
        "config.forwardedAllowIps — the operator override path is broken"
    )
