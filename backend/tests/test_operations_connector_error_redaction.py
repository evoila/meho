# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tier-1 redaction of free-text diagnostics in the error builders.

The ``operations/_errors.py`` result builders surface free text taken
from ``str(exc)`` or an upstream response body
(``extras["exception_message"]`` / ``extras["upstream_message"]`` /
``extras["detail"]`` and the ``error`` summary tails built from them).
That text used to be length-capped only; these tests pin the hardened
contract: every such string runs through the Tier-1 redactor **before**
the cap, so a credential-bearing exception or upstream body yields no
unredacted secret anywhere in the returned
:class:`~meho_backplane.connectors.OperationResult` -- and redaction
runs first so a secret straddling the cap boundary cannot survive as a
cleartext fragment the patterns no longer match.

Benign diagnostics must pass through unchanged: the sanitizer uses the
packaged default policy (credential-shaped patterns only -- no
UUID / IP / FQDN rules), so hosts, status lines, and remediation text
stay legible.
"""

from __future__ import annotations

import httpx
import pytest

from meho_backplane.operations._errors import (
    result_ambiguous_connector,
    result_connector_error,
    result_connector_http_403,
    result_connector_tls_verify_failed,
    result_no_connector,
)

# Credential fixtures assembled from fragments so gitleaks' built-in
# rules do not false-positive on the test source (the
# ``test_redaction_patterns.py`` posture).
_PASSWORD_SECRET = "P@ssw0rd" + "!withMore"
_PASSWORD_TEXT = "login failed for dsn admin:" + "password" + "=" + _PASSWORD_SECRET
_BEARER_SECRET = "eyJtoken" + "$value!123" + "abcdef"
_BEARER_TEXT = "request rejected; header was " + "Bearer " + _BEARER_SECRET


class _Target:
    host = "vcenter.lab.example.com"


def test_connector_error_redacts_labelled_password() -> None:
    """``str(exc)`` carrying ``password=...`` never reaches extras raw."""
    result = result_connector_error("op.read", RuntimeError(_PASSWORD_TEXT), 1.0)
    msg = result.extras["exception_message"]
    assert "[REDACTED:api_key]" in msg
    assert _PASSWORD_SECRET not in msg
    assert "P@ssw0rd" not in msg
    assert "withMore" not in msg
    # The structured envelope shape is unchanged.
    assert result.error == "connector_error: RuntimeError"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "RuntimeError"


def test_connector_error_redacts_bearer_token() -> None:
    """A bearer token inside the exception text is redacted."""
    result = result_connector_error("op.read", ValueError(_BEARER_TEXT), 1.0)
    msg = result.extras["exception_message"]
    assert "[REDACTED:bearer_token]" in msg
    assert _BEARER_SECRET not in msg


def test_connector_error_redacts_before_capping() -> None:
    """Redaction runs before the length cap.

    Capping first would truncate the secret mid-value into a fragment
    the ``api_key`` pattern no longer matches (the value run drops
    under its 8-char minimum), leaving cleartext in the envelope.
    """
    prefix = "x" * 250
    text = prefix + " " + "password" + "=" + _PASSWORD_SECRET
    result = result_connector_error("op.read", RuntimeError(text), 1.0)
    msg = result.extras["exception_message"]
    assert msg.endswith("...<truncated>")
    assert "P@ssw0rd" not in msg
    assert "withMore" not in msg


def test_connector_error_benign_message_unchanged() -> None:
    """No over-redaction: a benign diagnostic passes through verbatim."""
    text = "connection refused by 10.0.0.5 (vcenter.lab.example.com)"
    result = result_connector_error("op.read", RuntimeError(text), 1.0)
    # The default policy carries no UUID/IP/FQDN rules, so hosts stay
    # legible for diagnosis.
    assert result.extras["exception_message"] == text


def test_connector_error_long_benign_message_still_capped() -> None:
    """The 256-char cap discipline is preserved after redaction."""
    text = "boom " * 100
    result = result_connector_error("op.read", RuntimeError(text), 1.0)
    msg = result.extras["exception_message"]
    assert msg.endswith("...<truncated>")
    assert len(msg) == 256 + len("...<truncated>")


def test_tls_verify_failed_redacts_exception_message() -> None:
    """The TLS builder's ``exception_message`` is redacted too."""
    exc = ConnectionError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] via proxy " + _BEARER_TEXT,
    )
    result = result_connector_tls_verify_failed("op.read", exc, _Target(), 1.0)
    msg = result.extras["exception_message"]
    assert "[SSL: CERTIFICATE_VERIFY_FAILED]" in msg
    assert _BEARER_SECRET not in msg
    assert "[REDACTED:bearer_token]" in msg


def test_http_403_upstream_message_redacted_in_extras_and_summary() -> None:
    """Upstream body text is redacted before extras AND the summary tail."""
    request = httpx.Request("GET", "https://upstream.test/api")
    response = httpx.Response(
        403,
        json={"message": "denied for " + "token" + ": " + "ghp_secret!" + "value123"},
        request=request,
    )
    exc = httpx.HTTPStatusError("403", request=request, response=response)
    result = result_connector_http_403("op.write", exc, 1.0)
    upstream = result.extras["upstream_message"]
    assert "[REDACTED:api_key]" in upstream
    assert "ghp_secret" not in upstream
    assert "ghp_secret" not in (result.error or "")


@pytest.mark.parametrize("credential_text", [_PASSWORD_TEXT, _BEARER_TEXT])
def test_no_connector_exception_message_redacted(credential_text: str) -> None:
    """Resolver-diagnostic passthrough is sanitized at the builder."""
    result = result_no_connector(
        "op.read",
        "vsphere",
        "8.0",
        1.0,
        exception_message=credential_text,
    )
    msg = result.extras["exception_message"]
    assert "[REDACTED:" in msg
    assert _PASSWORD_SECRET not in msg
    assert _BEARER_SECRET not in msg


def test_ambiguous_connector_exception_message_redacted() -> None:
    """Same guarantee on the ambiguous-resolution sibling."""
    result = result_ambiguous_connector(
        "op.read",
        "vsphere",
        "8.0",
        "candidates=[a, b]; seen header " + "Bearer " + _BEARER_SECRET,
        1.0,
    )
    msg = result.extras["exception_message"]
    assert "candidates=[a, b]" in msg
    assert _BEARER_SECRET not in msg
    assert "[REDACTED:bearer_token]" in msg
