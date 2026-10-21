# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Entry point of the built-in core's ``bootstrap`` Helm hook Job.

The chart runs ``python -m meho_backplane.core.bootstrap <stage>`` after every
install and every upgrade (``deploy/charts/meho/templates/core-bootstrap-job.yaml``).
The one stage today is ``keycloak``, the realm stage of #4043
(:mod:`meho_backplane.core.keycloak`). Exit code ``0`` on success, ``1`` on
any failure with one sentence on stderr as ``core_bootstrap_failed: <sentence>``,
``2`` for a wrong command line. See docs/codebase/core-bootstrap.md.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence

from meho_backplane.core import keycloak
from meho_backplane.core.keycloak import RealmStageError

__all__ = ["STAGES", "main"]

STAGES: Mapping[str, Callable[[], Awaitable[None]]] = {"keycloak": keycloak.run}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in STAGES:
        names = ", ".join(STAGES)
        usage = f"usage: python -m meho_backplane.core.bootstrap <stage>; stages: {names}"
        print(usage, file=sys.stderr)
        return 2
    try:
        asyncio.run(STAGES[args[0]]())
    except RealmStageError as exc:
        print(f"core_bootstrap_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"core_bootstrap_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
