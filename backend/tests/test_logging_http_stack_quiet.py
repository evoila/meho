# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO writes no response header value of an HTTP call into a log line."""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import httpx
import httpx2
import openai
import pytest
import requests

from meho_backplane.logging import _HTTP_STACK_LOGGERS, configure_logging

_STACK_LOGGERS = (
    *_HTTP_STACK_LOGGERS,
    "httpcore.http11",
    "httpcore.connection",
    "urllib3.connectionpool",
    "botocore.endpoint",
    "google.auth.transport.requests",
    "httpcore2.http11",
    "httpcore2.connection",
    "anthropic._base_client",
    "openai._base_client",
)

_ANTHROPIC_REPLY = (
    b'{"id": "msg_1", "type": "message", "role": "assistant", "model": "m",'
    b' "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",'
    b' "stop_sequence": null, "usage": {"input_tokens": 1, "output_tokens": 1}}'
)
_OPENAI_REPLY = (
    b'{"id": "c1", "object": "chat.completion", "created": 0, "model": "m",'
    b' "choices": [{"index": 0, "finish_reason": "stop",'
    b' "message": {"role": "assistant", "content": "ok"}}]}'
)


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
        "prompt": f"m{secrets.token_hex(6)}",
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

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = _ANTHROPIC_REPLY if self.path.endswith("/messages") else _OPENAI_REPLY
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"sid={values['cookie']}")
            self.send_header("X-Vendor", values["header"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
    names = ("httpx", "httpx2", "httpcore2", "anthropic", "openai", *_STACK_LOGGERS)
    saved = {n: (logging.getLogger(n).level, logging.getLogger(n).disabled) for n in names}
    # An earlier logging.config call in the same test process can disable these
    # loggers; turn them on so the test sees what the level alone does.
    for n in names:
        logging.getLogger(n).disabled = False
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_root)
        for n, (lvl, disabled) in saved.items():
            logging.getLogger(n).setLevel(lvl)
            logging.getLogger(n).disabled = disabled


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


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_real_requests_call_logs_no_path_or_query(
    level: int, server: str, values: dict[str, str], captured: _ListHandler
) -> None:
    configure_logging(level)
    response = requests.get(
        f"{server}/{values['path']}?z={values['query']}", allow_redirects=False, timeout=5
    )
    assert response.status_code == 302
    text = "\n".join(captured.lines)
    for value in values.values():
        assert value not in text


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_real_httpx2_call_logs_no_query_or_cookie(
    level: int, server: str, values: dict[str, str], captured: _ListHandler
) -> None:
    configure_logging(level)
    with httpx2.Client(follow_redirects=False) as client:
        response = client.get(f"{server}/{values['path']}?z={values['query']}")
    assert response.status_code == 302
    text = "\n".join(captured.lines)
    for value in values.values():
        assert value not in text


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_real_ai_sdk_calls_log_no_prompt_or_header(
    level: int, server: str, values: dict[str, str], captured: _ListHandler
) -> None:
    configure_logging(level)
    prompt = f"say {values['prompt']}"
    with anthropic.Anthropic(base_url=server, api_key="test-key", max_retries=0) as client:
        reply = client.messages.create(
            model="m", max_tokens=8, messages=[{"role": "user", "content": prompt}]
        )
    assert reply.content[0].type == "text"
    with openai.OpenAI(base_url=f"{server}/v1", api_key="test-key", max_retries=0) as oai:
        completion = oai.chat.completions.create(
            model="m", messages=[{"role": "user", "content": prompt}]
        )
    assert completion.choices[0].message.content == "ok"
    text = "\n".join(captured.lines)
    for value in values.values():
        assert value not in text


@pytest.mark.parametrize("name", _STACK_LOGGERS)
def test_stack_logger_drops_debug_line_when_root_is_debug(
    name: str, values: dict[str, str], captured: _ListHandler
) -> None:
    configure_logging(logging.DEBUG)
    logger = logging.getLogger(name)
    assert logger.getEffectiveLevel() == logging.WARNING
    assert not logger.isEnabledFor(logging.INFO)
    logger.debug("GET /%s?k=%s", values["path"], values["query"])
    assert not any(values["path"] in line for line in captured.lines)
