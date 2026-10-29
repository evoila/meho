# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``redact_error_result`` cleans ``error``, ``extras`` and ``result`` (#5031).

An error a handler returns, and a multi-step operation's error envelope, carry
the vendor's detail in ``result``. The cleaner covers that field like the other
two. When the cleaning itself fails, the result keeps only its ``error_code``:
the payload and the handle are dropped.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from meho_backplane.connectors.schemas import OperationResult, ResultHandle
from meho_backplane.operations import _vendor_text
from meho_backplane.operations._vendor_text import ERROR_TEXT_WITHHELD, redact_error_result
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER

_PASSWORD = "made-up-pass-5031"
_TOKEN = "made-up-token-5031"


def _error_result(payload: dict[str, Any] | list[Any] | None) -> OperationResult:
    return OperationResult(
        status="error",
        op_id="vendor.item.create",
        result=payload,
        error=f"create_error: rejected password={_PASSWORD}",
        duration_ms=1.0,
        extras={"error_code": "create_error", "detail": {"password": _PASSWORD}},
    )


def _text(result: OperationResult) -> str:
    return result.model_dump_json()


def test_result_field_is_cleaned() -> None:
    envelope = {
        "status": "create_error",
        "issues": [
            {
                "severity": "error",
                "message": f'vendor said {{"sessionToken": "{_TOKEN}"}}',
                "rejected": {"password": _PASSWORD, "user": "svc"},
            }
        ],
        "account": {"encryptedPassword": _PASSWORD, "bind_secret_id": True},
    }
    out = redact_error_result(_error_result(envelope))
    text = _text(out)
    assert _PASSWORD not in text
    assert _TOKEN not in text
    assert out.result is not None and isinstance(out.result, dict)
    assert out.result["issues"][0]["rejected"] == {
        "password": SECRET_FIELD_MARKER,
        "user": "svc",
    }
    assert out.result["account"] == {
        "encryptedPassword": SECRET_FIELD_MARKER,
        "bind_secret_id": True,
    }
    assert out.extras["detail"] == {"password": SECRET_FIELD_MARKER}
    assert out.extras["error_code"] == "create_error"


def test_result_list_is_cleaned() -> None:
    payload = [{"password": _PASSWORD}, json.dumps({"token": _TOKEN})]
    out = redact_error_result(_error_result(payload))
    assert _PASSWORD not in _text(out)
    assert _TOKEN not in _text(out)
    assert out.result == [
        {"password": SECRET_FIELD_MARKER},
        f'{{"token": "{SECRET_FIELD_MARKER}"}}',
    ]


def test_result_none_stays_none() -> None:
    out = redact_error_result(_error_result(None))
    assert out.result is None
    assert _PASSWORD not in _text(out)


def test_failed_cleaning_drops_payload_and_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(node: Any, policy: Any) -> Any:
        raise RuntimeError("redaction unavailable")

    monkeypatch.setattr(_vendor_text, "_redacted", _boom)
    built = _error_result({"password": _PASSWORD}).model_copy(
        update={
            "handle": ResultHandle(
                handle_id=uuid.UUID("00000000-0000-0000-0000-000000005031"),
                summary_md="one row",
                schema_={"type": "object"},
                ttl_seconds=60,
            ),
            "delivery": "partial",
        }
    )
    out = redact_error_result(built)
    assert _PASSWORD not in _text(out)
    assert out.result is None
    assert out.handle is None
    assert out.delivery == "unavailable"
    assert out.error == ERROR_TEXT_WITHHELD
    assert dict(out.extras) == {"error_code": "create_error"}
    assert out.status == "error"
