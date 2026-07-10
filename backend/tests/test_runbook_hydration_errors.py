# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.runbooks.hydration_errors`.

The shared structured-error envelope both the REST route and the MCP tool
emit when hydrating a stored runbook template fails (Task #2239). Testing
the builder in isolation pins the shape one place so the two transports
can't drift.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from meho_backplane.runbooks.hydration_errors import (
    TEMPLATE_BODY_VALIDATION_FAILED,
    build_template_body_validation_detail,
)
from meho_backplane.runbooks.service import _steps_from_storage


def _hydration_error() -> ValidationError:
    """Return the real error a legacy empty-body row raises on hydration."""
    poisoned = [
        {
            "id": "revoke",
            "title": "Revoke",
            "body": "",
            "type": "manual",
            "verify": {"type": "confirm", "prompt": "Done?"},
        }
    ]
    try:
        _steps_from_storage(poisoned)  # type: ignore[arg-type]
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_envelope_carries_code_slug_version_and_errors() -> None:
    detail = build_template_body_validation_detail(
        slug="cert-rotate", version=3, exc=_hydration_error()
    )
    assert detail["error"] == TEMPLATE_BODY_VALIDATION_FAILED
    assert detail["slug"] == "cert-rotate"
    assert detail["version"] == 3
    errors = detail["errors"]
    assert isinstance(errors, list) and len(errors) == 1
    assert errors[0]["type"] == "string_too_short"
    assert errors[0]["loc"] == ["steps", 0, "manual", "body"]
    assert set(errors[0]) == {"type", "loc", "msg"}
    message = detail["message"]
    assert isinstance(message, str)
    assert "v3" in message
    assert "migration 0054" in message


def test_envelope_is_json_serialisable() -> None:
    """Both transports serialise the dict verbatim, so it must be JSON-safe.

    ``include_url`` / ``include_context`` / ``include_input`` are stripped
    from the pydantic errors so no non-serialisable object (an exception
    instance in ``ctx``, a raw input value) rides the envelope.
    """
    detail = build_template_body_validation_detail(slug="x", version=None, exc=_hydration_error())
    # Round-trips through json with no default= fallback.
    assert json.loads(json.dumps(detail)) == detail


def test_version_none_renders_latest_clause() -> None:
    detail = build_template_body_validation_detail(slug="x", version=None, exc=_hydration_error())
    assert detail["version"] is None
    assert "(latest version)" in detail["message"]  # type: ignore[operator]
