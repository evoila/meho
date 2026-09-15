# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Opt-in bearer-token guard for the operational exposition endpoints (#3499).

``GET /metrics`` (:mod:`meho_backplane.main`) and ``GET /ready``
(:mod:`meho_backplane.health`) are served on the shared ingress. Left
unauthenticated they leak operational metrics and the effective four-eyes /
feature-gate posture to anyone who can reach the ingress — on a shared instance
hosting an untrusted-visitor tenant (the Envision stand, meho-internal#320) that
is production activity exposed to the public booth.

:func:`verify_metrics_access` is the shared FastAPI dependency both endpoints
declare. It is **opt-in and default-open**: when
:attr:`~meho_backplane.settings.Settings.metrics_auth_token` is empty (the
default) it returns immediately, so behaviour is unchanged and the in-cluster
scraper + kubelet readiness probe keep working with no config change. When the
token is set it requires ``Authorization: Bearer <token>`` and refuses anything
else with ``401``.

Design notes:

* **Bearer, not source-CIDR.** Behind the shared ingress the app sees the proxy
  IP, not the real client, so a peer-IP CIDR check cannot separate a booth
  visitor from the scraper without trusting a spoofable ``X-Forwarded-For``. A
  bearer token is proxy-independent and is the standard Prometheus scrape auth
  (``bearer_token`` / ServiceMonitor ``bearerTokenSecret``); a kubelet
  readiness probe carries it via ``httpHeaders``.
* **``/healthz`` is never guarded.** It is a pure liveness signal (always 200,
  no posture), so a bearer-less liveness path always exists even when the guard
  is on.
* **Constant-time compare** (:func:`secrets.compare_digest` over UTF-8 bytes)
  so a wrong token cannot be recovered by timing; the presented value is never
  logged.
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import HTTPException, Request, status

from meho_backplane.settings import get_settings

__all__ = ["verify_metrics_access"]

_log = structlog.get_logger(__name__)


async def verify_metrics_access(request: Request) -> None:
    """Enforce the opt-in bearer-token guard on ``/metrics`` and ``/ready``.

    A no-op when ``metrics_auth_token`` is unset (default-open). Otherwise the
    request must present ``Authorization: Bearer <token>`` matching the
    configured token; a missing, malformed, or mismatched header raises ``401``
    with a ``WWW-Authenticate: Bearer`` challenge.
    """
    token = get_settings().metrics_auth_token
    if not token:
        return
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if (
        scheme.lower() != "bearer"
        or not presented
        or not secrets.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
    ):
        _log.warning("metrics_access_denied", path=request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="metrics_auth_required",
            headers={"WWW-Authenticate": "Bearer"},
        )
