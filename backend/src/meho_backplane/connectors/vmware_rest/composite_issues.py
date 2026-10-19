# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One shared builder for the ``issues[]`` entries vmware-rest composites return.

A composite reports trouble as a list of ``{category, severity, message}``
entries. ``severity == "error"`` is the marker the dispatcher reads to treat
the returned envelope as a failed operation
(:func:`~meho_backplane.operations.dispatcher._composite_terminal_error_code`,
#3809 / #3812): the result becomes ``status="error"`` and the audit row says
``error``, not ``ok``. ``warning`` / ``info`` entries stay on the success path.

Every composite builds its entries through :func:`issue` so the shape stays the
same across handlers (#3813). A handler whose mutation did not complete (a task
timeout, a rollback, a partial apply) must attach an ``error`` entry; a soft
refusal or an idempotency guard (``rule_exists``, ``not_powered_off``,
``no_change``) must not.
"""

from __future__ import annotations

from typing import Any


def issue(category: str, severity: str, message: str) -> dict[str, Any]:
    """Build one ``{category, severity, message}`` issue entry."""
    return {"category": category, "severity": severity, "message": message}


__all__ = ["issue"]
