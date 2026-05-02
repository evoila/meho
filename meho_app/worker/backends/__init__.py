# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Ingestion worker backend implementations.

Re-exports protocol types eagerly and backend classes lazily:
    from meho_app.worker.backends import IngestionBackend, LocalBackend, ...

Backend classes are lazy-imported to avoid loading heavy SDKs
(kubernetes-asyncio, google-cloud-run, docker) until actually needed.
"""

from meho_app.worker.backends.protocol import (
    IngestionBackend,
    JobState,
    JobStatus,
    ResourceProfile,
)

__all__ = [
    "CloudRunBackend",
    "DockerBackend",
    "IngestionBackend",
    "JobState",
    "JobStatus",
    "KubernetesBackend",
    "LocalBackend",
    "ResourceProfile",
]


def __getattr__(name: str) -> type:
    """Lazy-import backend classes to avoid loading heavy SDK dependencies.

    Args:
        name: The attribute name being accessed.

    Returns:
        The backend class.

    Raises:
        AttributeError: If the name is not a known backend class.
    """
    if name == "LocalBackend":
        from meho_app.worker.backends.local import LocalBackend

        return LocalBackend
    if name == "KubernetesBackend":
        from meho_app.worker.backends.kubernetes import KubernetesBackend

        return KubernetesBackend
    if name == "CloudRunBackend":
        from meho_app.worker.backends.cloudrun import CloudRunBackend

        return CloudRunBackend
    if name == "DockerBackend":
        from meho_app.worker.backends.docker import DockerBackend

        return DockerBackend
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
