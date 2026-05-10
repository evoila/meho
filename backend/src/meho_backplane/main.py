# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI application entrypoint.

This module exposes the ``app`` callable consumed by uvicorn /
Gunicorn / k8s probes. v0.1 ships the identity route plus the public
operator surfaces (``/healthz``, ``/version``, ``/ready``); structured
logs and Prometheus ``/metrics`` land in Task #20.
"""

from typing import Final

from fastapi import FastAPI

from meho_backplane import __version__
from meho_backplane.health import router as health_router
from meho_backplane.version import router as version_router

_APP_NAME: Final[str] = "meho-backplane"

app: FastAPI = FastAPI(
    title=_APP_NAME,
    version=__version__,
    description="MEHO governance-layer backplane (chassis-only in v0.1).",
)

app.include_router(health_router)
app.include_router(version_router)


@app.get("/")
async def root() -> dict[str, str]:
    """Identity route.

    Returns the running app's name and version. Kept alongside
    ``/healthz`` because some legacy probes hit ``/`` instead of
    ``/healthz`` and we want both paths to behave.
    """
    return {"name": _APP_NAME, "version": __version__}
