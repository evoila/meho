# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``python -m meho_backplane.runner`` — the satellite-runner entrypoint.

The third execution mode of the shared image, alongside the default
Serve mode (the central ``meho_backplane.main:app``) and Migrate
(``python -m meho_backplane.db.migrate``). Moulded on
:mod:`meho_backplane.db.migrate`: a flagless ``main() -> int`` that
returns a process exit code, invoked under ``sys.exit``. Behaviour is
governed entirely by ``MEHO_RUNNER_*`` env vars — no CLI flags, so an
operator cannot request a half-configured runner.

Exit codes:

* ``0`` — clean shutdown (SIGTERM/SIGINT cancelled the loop and it
  unwound), or an ordinary keyboard interrupt.
* ``1`` — configuration error (a required ``MEHO_RUNNER_*`` var is
  missing or malformed); the offending variable is named on stderr.
"""

from __future__ import annotations

import sys

from meho_backplane.runner.loop import run_runner
from meho_backplane.runner.settings import RunnerConfigError

__all__ = ["main"]


def main() -> int:
    """Run the satellite runner; return a process exit code."""
    try:
        run_runner()
    except RunnerConfigError as exc:
        print(f"runner_config_error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
