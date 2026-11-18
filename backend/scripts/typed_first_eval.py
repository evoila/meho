# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Start the typed-first search test; only the JSON report goes to stdout.

Importing :mod:`scripts.retrieval_eval` imports the backplane, and the
backplane logs to stdout while it loads. This module sends stdout to
stderr before that import, so ``> report.json`` gives a valid JSON file::

    uv run python -m scripts.typed_first_eval --tenant-id <tenant-uuid> > report.json

The test itself is :mod:`scripts.retrieval_eval.typed_first`.
"""

from __future__ import annotations

import contextlib
import sys


def _main() -> int:
    with contextlib.redirect_stdout(sys.stderr):
        from scripts.retrieval_eval.typed_first import main
    return main()


if __name__ == "__main__":
    sys.exit(_main())
