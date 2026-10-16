# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured ``extras.upstream_fault`` and the 2 KB ``upstream_message`` cap (#3567).

A vim fault body names the fault class (``NotAuthenticated``,
``InvalidLogin``, ...) and carries a ``faultstring``. Before #3567 the
error builders only surfaced the body as ``upstream_message`` text capped
at 256 chars, so the faultstring was often cut off and the fault class was
never a field of its own. These tests pin the new contract:

* a VI/JSON fault body and a SOAP ``<soapenv:Fault>`` body both yield
  ``extras.upstream_fault == {"type": ..., "faultstring": ...}`` with the
  full faultstring, on every builder that reads an upstream response;
* other bodies leave ``upstream_fault`` out, so their envelopes keep the
  exact old shape;
* ``upstream_message`` is capped at ``_UPSTREAM_MESSAGE_CAP`` (2 KB), not
  the 256-char ``_EXC_MESSAGE_CAP``;
* Tier-1 redaction still runs before the cap, on both fields.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from meho_backplane.connectors import OperationResult
from meho_backplane.operations._errors import (
    _EXC_MESSAGE_CAP,
    _UPSTREAM_MESSAGE_CAP,
    result_connector_auth_failed,
    result_connector_error,
    result_connector_http_403,
    result_connector_http_422,
)

# Credential fixture assembled from fragments so gitleaks' built-in rules
# do not false-positive on the test source.
_PASSWORD_SECRET = "P@ssw0rd" + "!withMore"
_PASSWORD_TEXT = "login failed for dsn admin:" + "password" + "=" + _PASSWORD_SECRET

_NOT_AUTHENTICATED = "The session is not authenticated."
_INVALID_LOGIN = "Cannot complete login due to an incorrect user name or password."


class _Target:
    name = "vc-a"
    host = "vcenter.example.test"
    secret_ref = None


def _vi_json_fault(faultstring: str = _NOT_AUTHENTICATED) -> dict[str, Any]:
    """A VI/JSON ``NotAuthenticated`` body whose faultstring sits past 256 chars."""
    return {
        "_typeName": "NotAuthenticated",
        "faultCause": None,
        "faultMessage": [],
        "object": {
            "_typeName": "ManagedObjectReference",
            "type": "GuestOperationsManager",
            "value": "guestOperationsManager-" + "x" * 300,
        },
        "privilegeId": "System.View",
        "faultstring": faultstring,
    }


def _soap_fault(faultstring: str = _INVALID_LOGIN, fault_type: str = "InvalidLogin") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<soapenv:Body><soapenv:Fault>"
        "<faultcode>ServerFaultCode</faultcode>"
        f"<faultstring>{faultstring}</faultstring>"
        f'<detail><{fault_type}Fault xmlns="urn:vim25" xsi:type="{fault_type}"/></detail>'
        "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    )


def _http_error(
    status_code: int,
    *,
    json_body: Any = None,
    text_body: str | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vcenter.example.test/sdk/vim25/x")
    if json_body is not None:
        response = httpx.Response(status_code, json=json_body, request=request)
    else:
        response = httpx.Response(status_code, text=text_body or "", request=request)
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


_Builder = Callable[[httpx.HTTPStatusError], OperationResult]

_BUILDERS: list[tuple[str, int, _Builder]] = [
    ("connector_error", 500, lambda exc: result_connector_error("op", exc, 1.0)),
    ("http_403", 403, lambda exc: result_connector_http_403("op", exc, 1.0)),
    ("http_422", 422, lambda exc: result_connector_http_422("op", exc, 1.0)),
    (
        "auth_failed",
        401,
        lambda exc: result_connector_auth_failed("op", exc, _Target(), duration_ms=1.0),
    ),
]
_BUILDER_IDS = [name for name, _, _ in _BUILDERS]


@pytest.mark.parametrize(("_name", "status", "build"), _BUILDERS, ids=_BUILDER_IDS)
def test_vi_json_not_authenticated_yields_upstream_fault(
    _name: str, status: int, build: _Builder
) -> None:
    """VI/JSON ``NotAuthenticated``: structured type + the full faultstring."""
    out = build(_http_error(status, json_body=_vi_json_fault()))
    assert out.extras["upstream_fault"] == {
        "type": "NotAuthenticated",
        "faultstring": _NOT_AUTHENTICATED,
    }
    # The raw body is longer than the old 256-char cap; it now survives whole.
    message = out.extras["upstream_message"]
    assert isinstance(message, str)
    assert len(message) > _EXC_MESSAGE_CAP
    assert _NOT_AUTHENTICATED in message
    assert not message.endswith("...<truncated>")


@pytest.mark.parametrize(("_name", "status", "build"), _BUILDERS, ids=_BUILDER_IDS)
def test_soap_invalid_login_yields_upstream_fault(_name: str, status: int, build: _Builder) -> None:
    """SOAP ``InvalidLogin``: the fault type comes from ``detail``'s ``xsi:type``."""
    out = build(_http_error(status, text_body=_soap_fault()))
    assert out.extras["upstream_fault"] == {
        "type": "InvalidLogin",
        "faultstring": _INVALID_LOGIN,
    }


def test_soap_fault_without_detail_keeps_faultstring() -> None:
    """A SOAP fault with no ``detail`` still surfaces its faultstring."""
    body = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body><soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
        "<faultstring>Permission to perform this operation was denied.</faultstring>"
        "</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    )
    out = result_connector_error("op", _http_error(500, text_body=body), 1.0)
    assert out.extras["upstream_fault"] == {
        "type": None,
        "faultstring": "Permission to perform this operation was denied.",
    }


def test_vi_json_fault_without_faultstring_keeps_type() -> None:
    out = result_connector_error(
        "op", _http_error(500, json_body={"_typeName": "InvalidArgument"}), 1.0
    )
    assert out.extras["upstream_fault"] == {"type": "InvalidArgument", "faultstring": None}


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_http_error(500, json_body={"message": "boom"}), id="github-style-json"),
        pytest.param(_http_error(500, json_body=[{"_typeName": "X"}]), id="json-list"),
        pytest.param(_http_error(500, json_body={"_typeName": "  "}), id="blank-type"),
        pytest.param(_http_error(500, text_body="Internal Server Error"), id="plain-text"),
        pytest.param(_http_error(500, text_body="<html><body>502</body></html>"), id="html"),
        pytest.param(_http_error(500, text_body="<not-closed"), id="broken-xml"),
        pytest.param(
            _http_error(
                500,
                text_body=(
                    '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]>'
                    "<soapenv:Envelope><soapenv:Body><soapenv:Fault>&e;</soapenv:Fault>"
                    "</soapenv:Body></soapenv:Envelope>"
                ),
            ),
            id="xml-entity-refused",
        ),
        pytest.param(_http_error(500, text_body=""), id="empty"),
    ],
)
def test_non_fault_body_leaves_upstream_fault_out(error: httpx.HTTPStatusError) -> None:
    """Non-fault bodies keep the old envelope shape: no ``upstream_fault`` key."""
    out = result_connector_error("op", error, 1.0)
    assert "upstream_fault" not in out.extras


def test_upstream_message_capped_at_upstream_cap() -> None:
    """``upstream_message`` uses its own 2 KB cap, not the 256-char exception cap."""
    assert _UPSTREAM_MESSAGE_CAP == 2048
    at_cap = result_connector_error(
        "op", _http_error(500, text_body="y" * _UPSTREAM_MESSAGE_CAP), 1.0
    )
    assert at_cap.extras["upstream_message"] == "y" * _UPSTREAM_MESSAGE_CAP

    over = result_connector_error(
        "op", _http_error(500, text_body="y" * (_UPSTREAM_MESSAGE_CAP + 1)), 1.0
    )
    message = over.extras["upstream_message"]
    assert message == "y" * _UPSTREAM_MESSAGE_CAP + "...<truncated>"
    # ``exception_message`` keeps the small cap.
    long_exc = RuntimeError("z" * 1000)
    assert len(result_connector_error("op", long_exc, 1.0).extras["exception_message"]) == (
        _EXC_MESSAGE_CAP + len("...<truncated>")
    )


def test_upstream_fault_faultstring_capped() -> None:
    out = result_connector_error("op", _http_error(500, json_body=_vi_json_fault("f" * 5000)), 1.0)
    faultstring = out.extras["upstream_fault"]["faultstring"]
    assert faultstring == "f" * _UPSTREAM_MESSAGE_CAP + "...<truncated>"


def test_redaction_runs_before_cap_on_both_fields() -> None:
    """A secret in the faultstring is redacted in both fields, even across the cap."""
    # The secret starts just before the cap: capping first would leave a
    # cleartext fragment the redaction patterns no longer match.
    faultstring = "a " * ((_UPSTREAM_MESSAGE_CAP - 40) // 2) + _PASSWORD_TEXT
    out = result_connector_error("op", _http_error(500, json_body=_vi_json_fault(faultstring)), 1.0)
    fault = out.extras["upstream_fault"]
    message = out.extras["upstream_message"]
    for text in (fault["faultstring"], message, out.error or ""):
        assert _PASSWORD_SECRET not in text
        assert "P@ssw0rd" not in text
    # The redaction marker itself is what the cap cuts, not the secret.
    assert "admin:[REDACTED:" in fault["faultstring"]
    assert fault["faultstring"].endswith("...<truncated>")
    assert fault["type"] == "NotAuthenticated"


def test_redaction_applies_to_soap_faultstring() -> None:
    out = result_connector_auth_failed(
        "op",
        _http_error(401, text_body=_soap_fault(faultstring=_PASSWORD_TEXT)),
        _Target(),
        duration_ms=1.0,
    )
    fault = out.extras["upstream_fault"]
    assert _PASSWORD_SECRET not in fault["faultstring"]
    assert "[REDACTED:api_key]" in fault["faultstring"]
    assert out.error is not None
    assert _PASSWORD_SECRET not in out.error


def test_non_http_connector_error_has_no_upstream_fields() -> None:
    out = result_connector_error("op", RuntimeError("boom"), 1.0)
    assert "upstream_fault" not in out.extras
    assert "upstream_message" not in out.extras
