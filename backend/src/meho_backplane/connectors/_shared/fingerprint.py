# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Redaction for direct-protocol connector fingerprint failure arms.

The DB-driver connectors (``mssql`` / ``postgres`` / ``mongodb``) degrade a
failed :meth:`fingerprint` to ``reachable=False`` with the failure recorded
under ``FingerprintResult.extras['error']`` and a paired
``*_fingerprint_unreachable`` warning log. The historical shape embedded the
raw driver exception verbatim::

    error=f"{type(exc).__name__}: {exc}"

For an authentication failure the driver's message carries the
**Vault-sourced username** -- pytds raises ``LoginError: Login failed for
user '<sql_username>'``; asyncpg and pymongo echo the same identity on their
auth rejections. That username then landed in both an audit-adjacent
``extras`` blob and a structured log line, verbatim (evoila/meho#3297).

This module is the single seam that ends that leak. A fingerprint failure
arm classifies the exception into one of a fixed set of reasons and calls
:func:`redact_fingerprint_error`, which emits the exception *type name* plus
the *classified reason* and never ``str(exc)``. The classification itself is
driver-specific (each connector's exception hierarchy differs) and stays in
the connector next to the ``probe()`` taxonomy it mirrors; only the safe
string shape is shared here.

The ``probe()`` paths already discard the driver message and are unchanged.
"""

from __future__ import annotations

from typing import Literal

#: The fixed set of classified fingerprint failure reasons. Mirrors the
#: ``reason`` values the direct-protocol connectors' ``probe()`` methods
#: already emit (``auth_failed`` / ``tcp_unreachable`` / ``connect_failed``)
#: so a target that degrades on ``fingerprint`` and one that degrades on
#: ``probe`` read consistently. Typed as a ``Literal`` so mypy rejects any
#: attempt to pass a free-form string (e.g. a regressed ``str(exc)``) as the
#: reason.
FingerprintFailureReason = Literal["auth_failed", "tcp_unreachable", "connect_failed"]


def redact_fingerprint_error(exc: BaseException, reason: FingerprintFailureReason) -> str:
    """Return a redacted fingerprint failure string: type name + classified reason.

    The returned string is safe to record in ``FingerprintResult.extras`` and
    to log: it names the exception class and the classified *reason*, and
    never interpolates ``str(exc)`` -- which, on an auth failure, echoes the
    Vault-sourced username (evoila/meho#3297).
    """
    return f"{type(exc).__name__}: {reason}"
