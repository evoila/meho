# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Helm pre-install / pre-upgrade Job entrypoint — ``alembic upgrade head``.

The chart wired by Goal #11's G2.5-T3 runs this module as a Kubernetes
Job before the Deployment rolls forward, so the schema is at ``head``
by the time pods come up. The contract is intentionally minimal:

* No CLI flags. Behaviour is governed entirely by ``DATABASE_URL`` and
  ``ALEMBIC_CONFIG`` (both already honoured by
  :mod:`meho_backplane.db.migrations`); operators can't accidentally
  request a partial upgrade. Forward-only is enforced by *not*
  exposing ``downgrade``.
* Exit ``0`` on success, ``1`` on any failure. The Job's
  ``backoffLimit`` retries on non-zero, but the Helm hook fails the
  release out cleanly when retries exhaust.
* Errors render to stderr as ``migration_failed: <ExcClass>: <msg>``.
  The exception class (not the message) is the structured key
  operators alert on; the message is for human triage and may carry
  schema-shape detail but never bearer-token-shaped content (the
  audit-side secret-leak sweep does not run inside this entrypoint;
  Alembic itself is the only thing logging here).

The runner shares :func:`meho_backplane.db.migrations.alembic_config`
with the readiness probe so the migration that *applies* and the
revision-comparison probe that *verifies* never disagree on which
``alembic.ini`` they targeted (env-var override → wheel package data
→ cwd → source tree).
"""

from __future__ import annotations

import sys

from alembic import command

from meho_backplane.db.migrations import alembic_config

__all__ = ["main"]


def main() -> int:
    """Run ``alembic upgrade head`` and return a process exit code.

    Returns ``0`` on success, ``1`` on any exception raised by
    Alembic (including the underlying SQLAlchemy / asyncpg / network
    errors propagated up from ``env.py``).
    """
    try:
        cfg = alembic_config()
        command.upgrade(cfg, "head")
    except Exception as exc:
        print(
            f"migration_failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
