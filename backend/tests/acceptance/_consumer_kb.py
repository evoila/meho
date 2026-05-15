# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Consumer kb-directory resolver for the G4.1 canary acceptance test.

The consumer's ``kb/`` directory (44+ entries spanning vSphere /
NSX / Vault / k8s / Argo / Harbor / etc.) lives in their separate
``evoila-bosnia/claude-rdc-hetzner-dc`` repo rather than ``evoila/meho``
itself, so the public chassis repo stays free of vendor-shaped
operator runbooks. This helper resolves a local path to the directory
by checking, in priority order:

1. The explicit ``MEHO_CONSUMER_KB_DIR`` env var. CI sets this from
   the runner's checkout of the consumer repo's ``kb/`` subdirectory.
2. The directory pointed at by ``MEHO_CONSUMER_DOCS_ROOT`` (the same
   env var the G0.7 vSphere canary consumes -- see
   :mod:`tests.acceptance._vcenter_spec`) which is expected to
   contain a ``kb/`` subdirectory. Local-dev convenience for the
   maintainer who has the consumer repo cloned at a known sibling
   path with both the spec shelf and the kb/ directory in one tree.

When no source resolves, the test skips with a pointer to this
docstring rather than failing -- the canary verifies a substrate
operators run from their own deploys, and the acceptance criterion
is re-evaluated in CI where the env vars are wired up. CI green stays
the operator-visible signal.

Why an env-var resolver rather than vendoring the corpus into
``backend/tests/fixtures/``: the consumer's kb is operator-curated
content tracked in their own repo; vendoring would fork the corpus
and any drift between MEHO's fixture copy and the consumer's live
copy would silently break the canary's "real-corpus" promise. The
env-var indirection keeps the corpus authoritative on the consumer
side; MEHO points at whatever the operator's checkout currently
holds.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "CONSUMER_KB_REASON",
    "resolve_consumer_kb_dir",
]

#: Documented reason the test skips when no consumer kb source is
#: configured. Embedded into the ``pytest.skip`` call so CI logs make
#: the missing env var traceable to this helper.
CONSUMER_KB_REASON = (
    "Consumer kb directory not configured. Set MEHO_CONSUMER_KB_DIR to the "
    "absolute path of the consumer's kb/ directory, or set "
    "MEHO_CONSUMER_DOCS_ROOT to a directory containing a kb/ subdirectory. "
    "See tests/acceptance/_consumer_kb.py for the resolver contract."
)


def resolve_consumer_kb_dir() -> Path | None:
    """Return the local path to the consumer's ``kb/`` directory, or ``None``.

    Checks the env vars described in the module docstring, in priority
    order. Returns ``None`` when nothing is configured so callers can
    convert that into a ``pytest.skip`` with :data:`CONSUMER_KB_REASON`.
    """
    explicit = os.getenv("MEHO_CONSUMER_KB_DIR")
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_dir():
            return candidate

    consumer_root = os.getenv("MEHO_CONSUMER_DOCS_ROOT")
    if consumer_root:
        candidate = Path(consumer_root).expanduser() / "kb"
        if candidate.is_dir():
            return candidate

    return None
