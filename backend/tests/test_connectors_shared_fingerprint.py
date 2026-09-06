# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the direct-protocol fingerprint error redaction seam (#3297).

:func:`redact_fingerprint_error` is the single shared helper the ``mssql`` /
``postgres`` / ``mongodb`` fingerprint failure arms use to build the
``extras['error']`` value and the paired warning-log ``error`` field. Its one
job is to *never* interpolate ``str(exc)`` -- which, on an auth failure,
echoes the Vault-sourced username -- so these tests pin that guarantee on a
worst-case exception whose message is the leak we are preventing.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors._shared.fingerprint import (
    FingerprintFailureReason,
    redact_fingerprint_error,
)


class _LoginError(Exception):
    """Stand-in for a driver auth exception whose message carries a username."""


def test_emits_type_name_and_reason_only() -> None:
    exc = _LoginError("Login failed for user 'sql_svc_acct'")
    assert redact_fingerprint_error(exc, "auth_failed") == "_LoginError: auth_failed"


@pytest.mark.parametrize(
    "reason",
    ["auth_failed", "tcp_unreachable", "connect_failed"],
)
def test_never_embeds_the_raw_message(reason: FingerprintFailureReason) -> None:
    """The username in the exception message never survives redaction."""
    exc = _LoginError("Login failed for user 'leaky_username'")
    redacted = redact_fingerprint_error(exc, reason)
    assert redacted == f"_LoginError: {reason}"
    assert "leaky_username" not in redacted
    assert str(exc) not in redacted


def test_uses_the_exception_type_not_the_instance() -> None:
    """A BaseException (not just Exception) subclass is reduced to its type name."""

    class _TimeoutError(TimeoutError):
        pass

    assert redact_fingerprint_error(
        _TimeoutError("host 10.0.0.9 timed out"), "tcp_unreachable"
    ) == ("_TimeoutError: tcp_unreachable")
