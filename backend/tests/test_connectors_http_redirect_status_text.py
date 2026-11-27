# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What an answer of a typed HTTP connector shows after a redirect.

A small typed HTTP connector runs one read operation through the real
dispatcher against a local HTTP server.

- MEHO follows a redirect on the same server, and the next answer is a 404,
  a 429 or a 500. ``extras.exception_message`` names only the status and the
  host name. The vendor's own error text stays.
- MEHO does not follow a redirect. The vendor's text of that answer is not
  passed on: one fixed sentence stands in its place.

Made-up values sit in the path, the query (also a ticket value), the
fragment and the user part of each address. The tests check the answer,
every column of the audit row, the stored trace and its spans, the agent's
copy of the trace, and the log output at DEBUG and above.
"""

from __future__ import annotations

import secrets

import pytest

from meho_backplane.connectors._shared.log_host import REDIRECT_TEXT_WITHHELD
from tests._http_redirect_harness import (  # noqa: F401 -- the autouse fixtures apply by import
    _OTHER_HOST,
    _START,
    _agent_trace_store,
    _assert_absent,
    _made_up,
    _reset_module_state,
    _run,
    _settings_env,
    _stub_embedding,
)

_VENDOR_WORDS = "the vendor could not find this thing"


def _ticket() -> tuple[str, list[str]]:
    """A made-up ticket in the Proxmox shape, and its made-up parts."""
    user, hex_part, signature = _made_up(), secrets.token_hex(4).upper(), _made_up()
    return f"PVE:{user}@pve:{hex_part}::{signature}", [user, hex_part, signature]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_line", "text"),
    [
        ("404 Not Found", "Client error '404 Not Found'"),
        ("429 Too Many Requests", "Client error '429 Too Many Requests'"),
        ("500 Internal Server Error", "Server error '500 Internal Server Error'"),
    ],
)
@pytest.mark.parametrize("trace", [True, False], ids=["trace-on", "trace-off"])
async def test_error_after_a_followed_redirect_names_only_the_host(
    caplog: pytest.LogCaptureFixture, status_line: str, text: str, trace: bool
) -> None:
    path, user, password, fragment = (_made_up() for _ in range(4))
    ticket, ticket_parts = _ticket()
    hop = f"/{path}?ticket={ticket}"
    # The server puts its own port in place of "{port}".
    location = f"http://{user}:{password}@127.0.0.1:{{port}}{hop}#{fragment}"
    routes = {_START: {"Location": location}, hop: {}}

    body = f'{{"message": "{_VENDOR_WORDS}"}}'.encode()
    result, places, _events = await _run(
        caplog,
        routes,
        trace=trace,
        route_status={_START: "307 Temporary Redirect", hop: status_line},
        route_body={hop: body},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_class"] == "HTTPStatusError"
    assert result.extras["http_status"] == int(status_line.split()[0])
    assert result.extras["exception_message"] == (
        f"{text} from 127.0.0.1, after a redirect on the same host."
    )
    assert result.extras["upstream_message"] == _VENDOR_WORDS
    _assert_absent([path, user, password, fragment, *ticket_parts], places)


def _address() -> tuple[str, list[str]]:
    """An address on another host with made-up values, and those values."""
    user, password, path, fragment = (_made_up() for _ in range(4))
    ticket, ticket_parts = _ticket()
    address = f"http://{user}:{password}@{_OTHER_HOST}:8443/{path}?ticket={ticket}#{fragment}"
    return address, [user, password, path, fragment, *ticket_parts]


def _bodies(address: str) -> dict[str, tuple[str, bytes]]:
    """Answer bodies of a redirect that repeat *address*, by name: (content type, body)."""
    escaped = address.replace("/", "\\/")
    soap = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body><soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
        f"<faultstring>Moved to {address}</faultstring></soapenv:Fault></soapenv:Body>"
        "</soapenv:Envelope>"
    )
    return {
        "json message": ("application/json", f'{{"message": "Moved to {address}"}}'.encode()),
        "json escaped": ("application/json", f'{{"location": "{escaped}"}}'.encode()),
        "vim fault": (
            "application/json",
            f'{{"_typeName": "NotFound", "faultstring": "See {address}"}}'.encode(),
        ),
        "soap fault": ("text/xml", soap.encode()),
        "plain text": ("text/plain", f"The thing moved to {address.lower()}".encode()),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_name", ["json message", "json escaped", "vim fault", "soap fault", "plain text"]
)
async def test_refused_redirect_passes_on_no_vendor_text(
    caplog: pytest.LogCaptureFixture, body_name: str
) -> None:
    address, values = _address()
    content_type, body = _bodies(address)[body_name]
    routes = {_START: {"Location": address, "Content-Type": content_type}}

    result, places, _events = await _run(caplog, routes, route_body={_START: body})

    assert result.status == "error"
    assert result.extras["http_status"] == 302
    assert result.extras["exception_message"] == (
        f"Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    assert result.extras["upstream_message"] == REDIRECT_TEXT_WITHHELD
    assert "upstream_fault" not in result.extras
    assert REDIRECT_TEXT_WITHHELD in places["audit row"]
    assert REDIRECT_TEXT_WITHHELD in places["stored trace"]
    _assert_absent([value.lower() for value in values] + values, places)
