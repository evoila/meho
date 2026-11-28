# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The five kinds of caller, in one list.

:func:`meho_backplane.auth.caller_kind.caller_kind` gives one of these
kinds, and the CHECK on ``audit_log.caller_kind`` in
:mod:`meho_backplane.db.models` allows only these kinds.

This module imports nothing from MEHO. It is outside the ``auth``
package, because importing ``auth`` loads :mod:`meho_backplane.db.models`.
So the audit model can import it without a cycle.
"""

from __future__ import annotations

from typing import Final

__all__ = ["CALLER_KINDS"]

#: Every kind of caller. ``auth/caller_kind.py`` gives this tuple as its
#: ``CALLER_KINDS``.
CALLER_KINDS: Final = (
    "person",
    "personal_agent",
    "service_agent",
    "runner",
    "system",
)
