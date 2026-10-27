# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Errors of the FortiOS connector, and how an HTTP error becomes one.

Every message here is built by the connector from cleaned text: the vendor's
own error text passes :func:`~meho_backplane.connectors.fortios.redaction.scrub_text`
first, and no ``raise ... from`` keeps the raw HTTP answer attached. So an
error text never holds a secret from the device.
"""

from __future__ import annotations

import json
from typing import Any, Final

import httpx

from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.fortios.redaction import scrub, scrub_text

__all__ = [
    "FortiOSApiError",
    "FortiOSScopeError",
    "FortiOSTargetError",
    "FortiOSVersionError",
    "auth_error",
    "error_from_status",
    "vendor_error_text",
]

#: Longest vendor error text kept in a message.
_TEXT_LIMIT: Final[int] = 300


class FortiOSApiError(ConnectorUpstreamError):
    """FortiOS answered with an error: an HTTP error, or ``status`` other than ``success``."""


class FortiOSTargetError(ValueError):
    """The target's settings do not describe a FortiGate device or VDOM target."""


class FortiOSScopeError(ValueError):
    """A device operation was called on a VDOM target, or the other way round."""


class FortiOSVersionError(RuntimeError):
    """The device runs a FortiOS version this connector does not cover."""


def _short(text: str) -> str:
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "..."


def vendor_error_text(payload: Any) -> str | None:
    """Return the cleaned error text of a FortiOS answer, or ``None``.

    FortiOS puts an error number under ``error`` and, for a refused change,
    the CLI's message under ``cli_error``.
    """
    if not isinstance(payload, dict):
        return None
    cleaned, _ = scrub(payload)
    parts: list[str] = []
    code = cleaned.get("error")
    if code not in (None, ""):
        parts.append(f"error {code}")
    cli_error = cleaned.get("cli_error")
    if isinstance(cli_error, str) and cli_error.strip():
        parts.append(_short(scrub_text(cli_error.strip())))
    return "; ".join(parts) or None


def _body_text(response: httpx.Response) -> str | None:
    try:
        payload = json.loads(response.content or b"null")
    except ValueError:
        return None
    return vendor_error_text(payload)


def auth_error(status_code: int, *, target: Any, api_path: str) -> ConnectorAuthError:
    """The error for a 401 (token refused) or a 403 (missing rights) answer."""
    name = getattr(target, "name", None)
    if status_code == 401:
        return ConnectorAuthError(
            f"FortiGate target {name!r} refused the API token (HTTP 401) on {api_path}. "
            "MEHO does not retry this call.",
            status_code=401,
            cause="token_refused",
            target_name=name,
            stage="call",
            remediation=(
                "Create a new API token for the REST API administrator on the FortiGate and "
                "store it as api_token at the target's secret_ref. Check that the "
                "administrator's trusted hosts include MEHO's address."
            ),
        )
    return ConnectorAuthError(
        f"FortiGate target {name!r} accepted the API token but refused {api_path} "
        "(HTTP 403): the administrator profile of the token lacks the rights for this call.",
        status_code=403,
        cause="missing_rights",
        target_name=name,
        stage="call",
        remediation=(
            "Give the REST API administrator's profile the rights this operation needs "
            "(read for reads), and for a VDOM target make sure the token may use that VDOM."
        ),
    )


def error_from_status(
    exc: httpx.HTTPStatusError, *, target: Any, api_path: str
) -> ConnectorAuthError | FortiOSApiError:
    """Turn an HTTP error answer into a connector error with a clean message."""
    status = exc.response.status_code
    if status in (401, 403):
        return auth_error(status, target=target, api_path=api_path)
    detail = _body_text(exc.response)
    message = f"FortiOS answered HTTP {status} for {api_path}"
    if detail:
        message = f"{message}: {detail}"
    return FortiOSApiError(message, http_status=status, upstream_message=detail)
