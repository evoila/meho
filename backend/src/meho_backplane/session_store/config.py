# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Settings of the agent session store, read from the environment.

The store needs three values to work. Without them every session route
answers ``503 session_store_not_configured``:

* ``SESSION_STORE_ENDPOINT`` -- the URL of the S3-compatible object
  storage, for example ``https://objects.example.com``.
* ``SESSION_STORE_BUCKET`` -- the bucket (the top folder) for the files.
* ``SESSION_STORE_CREDENTIALS_VAULT_PATH`` -- the Vault KV-v2 API path
  (``secret/data/...``) that holds ``access_key_id`` and
  ``secret_access_key``. MEHO reads it with its scheduler token. There is
  no setting for a plain key value.

The other values have defaults:

* ``SESSION_STORE_REGION`` (``us-east-1``).
* ``SESSION_STORE_RETENTION_DAYS_DEFAULT`` (``90``) -- the retention
  period of a tenant that did not set its own.
* ``SESSION_STORE_MAX_UPLOAD_BYTES`` (25 MiB) -- the largest compressed
  archive MEHO takes.
* ``SESSION_STORE_MAX_UNPACKED_BYTES`` (64 MiB) and
  ``SESSION_STORE_MAX_FILES`` (2000) -- the limits of one unpacked
  archive.
* ``SESSION_STORE_CLEANUP_ENABLED`` (``true``) and
  ``SESSION_STORE_CLEANUP_INTERVAL_SECONDS`` (3600) -- the cleanup job.

The values live here, not in :mod:`meho_backplane.settings`, so this
feature adds a file instead of lines to a shared one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from meho_backplane.settings import parse_bool_env

__all__ = ["SessionStoreConfig", "load_config"]

_MIB: Final[int] = 1024 * 1024


@dataclass(frozen=True)
class SessionStoreConfig:
    """The settings of the session store."""

    endpoint: str
    bucket: str
    credentials_vault_path: str
    region: str
    retention_days_default: int
    max_upload_bytes: int
    max_unpacked_bytes: int
    max_files: int
    cleanup_enabled: bool
    cleanup_interval_seconds: int

    @property
    def configured(self) -> bool:
        """Whether the endpoint, the bucket and the credentials path are all set."""
        return bool(self.endpoint and self.bucket and self.credentials_vault_path)


def _int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    value = int(raw) if raw else default
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def load_config() -> SessionStoreConfig:
    """Read the settings from the environment. Cheap, so it is not cached."""
    return SessionStoreConfig(
        endpoint=os.environ.get("SESSION_STORE_ENDPOINT", "").strip(),
        bucket=os.environ.get("SESSION_STORE_BUCKET", "").strip(),
        credentials_vault_path=os.environ.get("SESSION_STORE_CREDENTIALS_VAULT_PATH", "").strip(),
        region=os.environ.get("SESSION_STORE_REGION", "").strip() or "us-east-1",
        retention_days_default=_int("SESSION_STORE_RETENTION_DAYS_DEFAULT", 90),
        max_upload_bytes=_int("SESSION_STORE_MAX_UPLOAD_BYTES", 25 * _MIB),
        max_unpacked_bytes=_int("SESSION_STORE_MAX_UNPACKED_BYTES", 64 * _MIB),
        max_files=_int("SESSION_STORE_MAX_FILES", 2000),
        cleanup_enabled=parse_bool_env(os.environ.get("SESSION_STORE_CLEANUP_ENABLED", "true")),
        cleanup_interval_seconds=_int("SESSION_STORE_CLEANUP_INTERVAL_SECONDS", 3600, minimum=60),
    )
