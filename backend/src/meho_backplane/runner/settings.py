# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Runner-mode configuration — its own env-var namespace.

The satellite runner cannot instantiate the chassis
:class:`~meho_backplane.settings.Settings`: that model hard-requires
Keycloak issuer / audience and a ``DATABASE_URL``, none of which a
push-only runner has. :class:`RunnerSettings` is the deliberately
separate model, moulded on the chassis ``Settings`` / ``get_settings``
pattern (a plain pydantic model plus an ``lru_cache`` accessor that maps
``os.environ`` explicitly, so the env-var contract is obvious in one
place) but namespaced under ``MEHO_RUNNER_*``.

A missing or malformed required variable raises :class:`RunnerConfigError`
naming the offending variable so the ``python -m meho_backplane.runner``
entrypoint can exit 1 with an actionable message.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "DEFAULT_SPOOL_DIR",
    "DEFAULT_SPOOL_MAX_FILES",
    "DEFAULT_TICK_INTERVAL_SECONDS",
    "RunnerConfigError",
    "RunnerSettings",
    "get_runner_settings",
]

#: Default cadence between tick sweeps. Overridable per deploy.
DEFAULT_TICK_INTERVAL_SECONDS = 60.0

#: Default on-disk retry-spool directory. Deliberately under
#: ``/var/lib`` (mutable runtime state) rather than the baked-content
#: ``/opt/meho`` convention the chassis uses for read-only assets.
DEFAULT_SPOOL_DIR = "/var/lib/meho/runner-spool"

#: Default cap on spooled batch files. Bounds disk growth on a runner
#: whose uplink to central stays partitioned for a long time.
DEFAULT_SPOOL_MAX_FILES = 1000


class RunnerConfigError(ValueError):
    """A required runner env var is missing, empty, or malformed.

    Subclasses :class:`ValueError`. The message always names the
    offending variable so the entrypoint's stderr line is actionable.
    """


class RunnerSettings(BaseModel):
    """Configuration for a satellite-runner process.

    Frozen so the resolved config cannot drift mid-process. ``runner_token``
    is ``repr``-excluded (mould: chassis secret fields) so it never lands
    in a structured log line via an accidental ``logger.bind(settings=...)``.
    """

    model_config = ConfigDict(frozen=True)

    central_url: str = Field(min_length=1)
    runner_id: str = Field(min_length=1)
    runner_token: str = Field(min_length=1, repr=False)
    tick_interval_seconds: float = Field(gt=0.0)
    spool_dir: str = Field(min_length=1)
    spool_max_files: int = Field(gt=0)


def _require_env(name: str) -> str:
    """Return ``os.environ[name]`` or raise :class:`RunnerConfigError`."""
    try:
        value = os.environ[name]
    except KeyError:
        raise RunnerConfigError(f"missing required env var: {name}") from None
    if not value.strip():
        raise RunnerConfigError(f"required env var {name} is empty")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RunnerConfigError(f"env var {name} must be a number: {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RunnerConfigError(f"env var {name} must be an integer: {raw!r}") from exc


@lru_cache(maxsize=1)
def get_runner_settings() -> RunnerSettings:
    """Return the process-wide :class:`RunnerSettings`, read from env.

    Reads ``MEHO_RUNNER_*`` on first call; subsequent calls return the
    cached instance. Tests that mutate ``os.environ`` call
    ``get_runner_settings.cache_clear()`` to force a re-read.

    Raises:
        RunnerConfigError: a required variable is missing/empty, a
            numeric variable is malformed, or the assembled config fails
            model validation.
    """
    try:
        return RunnerSettings(
            central_url=_require_env("MEHO_RUNNER_CENTRAL_URL"),
            runner_id=_require_env("MEHO_RUNNER_ID"),
            runner_token=_require_env("MEHO_RUNNER_TOKEN"),
            tick_interval_seconds=_env_float(
                "MEHO_RUNNER_TICK_INTERVAL_SECONDS", DEFAULT_TICK_INTERVAL_SECONDS
            ),
            spool_dir=os.environ.get("MEHO_RUNNER_SPOOL_DIR", DEFAULT_SPOOL_DIR),
            spool_max_files=_env_int("MEHO_RUNNER_SPOOL_MAX_FILES", DEFAULT_SPOOL_MAX_FILES),
        )
    except ValidationError as exc:
        raise RunnerConfigError(f"invalid runner configuration: {exc}") from exc
