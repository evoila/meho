# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""FastAPI application entrypoint.

This module exposes the ``app`` callable consumed by uvicorn /
Gunicorn / k8s probes. v0.1 ships only a root identity route — health
endpoints (``/healthz``, ``/version``, ``/ready``), structured logs,
and Prometheus metrics land in subsequent G2.1 Tasks (#19, #20).
"""

from typing import Final

from fastapi import FastAPI

from meho_backplane import __version__

_APP_NAME: Final[str] = "meho-backplane"

app: FastAPI = FastAPI(
    title=_APP_NAME,
    version=__version__,
    description="MEHO governance-layer backplane (chassis-only in v0.1).",
)


@app.get("/")
async def root() -> dict[str, str]:
    """Identity route.

    Returns the running app's name and version. Acts as a smoke probe
    for ``uvicorn`` / container startup until ``/healthz`` is wired in
    Task #19.
    """
    return {"name": _APP_NAME, "version": __version__}
