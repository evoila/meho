# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO writes no response header value of an HTTP call into a log line."""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from meho_backplane.logging import configure_logging

_STACK_LOGGERS = ("httpcore", "httpcore.http11", "httpcore.connection", "h2", "hpack")


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def values() -> dict[str, str]:
    return {
        "path": f"p{secrets.token_hex(6)}",
        "query": f"q{secrets.token_hex(6)}",
        "cookie": f"c{secrets.token_hex(6)}",
        "header": f"h{secrets.token_hex(6)}",
    }


@pytest.fixture
def server(values: dict[str, str]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"/{values['path']}?k={values['query']}")
            self.send_header("Set-Cookie", f"sid={values['cookie']}")
            self.send_header("X-Vendor", values["header"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def captured() -> Iterator[_ListHandler]:
    handler = _ListHandler()
    root = logging.getLogger()
    saved_root = root.level
    saved = {n: logging.getLogger(n).level for n in ("httpx", *_STACK_LOGGERS)}
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_root)
        for n, lvl in saved.items():
            logging.getLogger(n).setLevel(lvl)


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_real_call_logs_no_response_value(
    level: int, server: str, values: dict[str, str], captured: _ListHandler
) -> None:
    configure_logging(level)
    logging.getLogger("hpack").debug("header: location %s", values["query"])
    logging.getLogger("h2").debug("header: set-cookie %s", values["cookie"])
    with httpx.Client(follow_redirects=False) as client:
        response = client.get(f"{server}/start?z={values['query']}")
    assert response.status_code == 302
    text = "\n".join(captured.lines)
    for value in values.values():
        assert value not in text


@pytest.mark.parametrize("name", _STACK_LOGGERS)
def test_stack_logger_holds_warning_when_root_is_debug(name: str, captured: _ListHandler) -> None:
    configure_logging(logging.DEBUG)
    logger = logging.getLogger(name)
    assert logger.getEffectiveLevel() == logging.WARNING
    assert not logger.isEnabledFor(logging.DEBUG)
    assert not logger.isEnabledFor(logging.INFO)
