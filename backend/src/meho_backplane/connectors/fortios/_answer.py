# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The FortiOS answer wrapper.

Every FortiOS REST answer is a JSON object with the payload under
``results`` and a wrapper around it: ``status`` (``success`` or ``error``),
``http_status``, ``vdom``, ``serial`` (the unit that answered), ``version``
(``v7.4.4``) and ``build``. An error answer adds ``error`` (a number) and,
for a refused change, ``cli_error``. The connector checks ``status``, not
only the HTTP code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from meho_backplane.connectors.fortios._errors import FortiOSApiError, vendor_error_text
from meho_backplane.connectors.fortios.redaction import scrub

__all__ = ["FortiOSAnswer", "unwrap_answer"]


@dataclass(frozen=True)
class FortiOSAnswer:
    """One checked FortiOS answer. ``results`` has its secrets removed."""

    results: Any
    version: str | None
    build: int | None
    serial: str | None
    vdom: str | None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def unwrap_answer(payload: Any, api_path: str) -> FortiOSAnswer:
    """Check the wrapper of *payload* and return it, or raise :class:`FortiOSApiError`."""
    if not isinstance(payload, dict):
        raise FortiOSApiError(
            f"FortiOS answer for {api_path} is not a JSON object",
            http_status=200,
            upstream_message=None,
        )
    status = payload.get("status")
    if status != "success":
        detail = vendor_error_text(payload)
        http_status = payload.get("http_status")
        message = f"FortiOS answered status {status!r} for {api_path}"
        if detail:
            message = f"{message}: {detail}"
        raise FortiOSApiError(
            message,
            http_status=http_status if isinstance(http_status, int) else 200,
            upstream_message=detail,
        )
    results, _ = scrub(payload.get("results"))
    build = payload.get("build")
    return FortiOSAnswer(
        results=results,
        version=_text(payload.get("version")),
        build=build if isinstance(build, int) else None,
        serial=_text(payload.get("serial")),
        vdom=_text(payload.get("vdom")),
    )
