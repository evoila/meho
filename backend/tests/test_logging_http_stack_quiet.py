# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests that no made-up path, query, cookie, header value or prompt text of an
HTTP call reaches MEHO's log output, and that each HTTP library logger is held
at its level."""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import httpx
import httpx2
import openai
import pytest
import requests
from requests.adapters import HTTPAdapter

from meho_backplane.logging import _HTTP_STACK_LOGGERS, _SAFE_LINES, configure_logging

_CHILD_LOGGERS = (
    "httpcore.http11",
    "httpcore.connection",
    "urllib3.connectionpool",
    "urllib3.connection",
    "botocore.endpoint",
    "botocore.credentials",
    "botocore.tokens",
    "google.auth.transport.requests",
    "httpcore2.http11",
    "httpcore2.connection",
    "anthropic._base_client",
    "openai._base_client",
)
_STACK_LOGGERS = (*_HTTP_STACK_LOGGERS, *_CHILD_LOGGERS)
_TOP_LOGGERS = tuple(dict.fromkeys(n.split(".")[0] for n in _STACK_LOGGERS))
_CHANGED_LOGGERS = ("", "httpx", *_STACK_LOGGERS, *_TOP_LOGGERS)
_ROOT_LEVELS = [logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING]

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


def _held_for(name: str) -> int:
    while name not in _HTTP_STACK_LOGGERS:
        name = name.rpartition(".")[0]
    return _HTTP_STACK_LOGGERS[name]


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
            if self.path.startswith("/broken"):
                # A header line without a colon, then a cookie header.
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\nbroken " + values["header"].encode() + b"\r\n"
                    b"Set-Cookie: sid=" + values["cookie"].encode() + b"\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
                return
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
def closing_server() -> Iterator[str]:
    """A server that reads each request and closes the connection without an answer."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            with conn:
                conn.recv(65536)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(timeout=5)
        sock.close()


def _logger_states() -> dict[str, tuple[int, bool]]:
    return {
        n: (logging.getLogger(n).level, logging.getLogger(n).disabled) for n in _CHANGED_LOGGERS
    }


@pytest.fixture(autouse=True, scope="module")
def _loggers_unchanged_after_module() -> Iterator[None]:
    """Check at the end of the module that each logger has its level from before."""
    before = _logger_states()
    yield
    assert _logger_states() == before


@pytest.fixture(autouse=True)
def _restore_loggers() -> Iterator[None]:
    saved = _logger_states()
    # An earlier logging.config call in the same test process can disable these
    # loggers; turn them on so the test sees what MEHO's setup alone does.
    for n in _CHANGED_LOGGERS:
        logging.getLogger(n).disabled = False
    try:
        yield
    finally:
        for n, (lvl, disabled) in saved.items():
            logging.getLogger(n).setLevel(lvl)
            logging.getLogger(n).disabled = disabled


def _assert_clean(output: str, values: dict[str, str]) -> None:
    for value in values.values():
        assert value not in output


@pytest.mark.parametrize("level", _ROOT_LEVELS)
def test_real_call_logs_no_response_value(
    level: int, server: str, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level)
    logging.getLogger("hpack").debug("header: location %s", values["query"])
    logging.getLogger("h2").debug("header: set-cookie %s", values["cookie"])
    with httpx.Client(follow_redirects=False) as client:
        response = client.get(f"{server}/start?z={values['query']}")
    assert response.status_code == 302
    _assert_clean(capfd.readouterr().out, values)


@pytest.mark.parametrize("level", _ROOT_LEVELS)
def test_real_requests_call_logs_no_path_or_query(
    level: int, server: str, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level)
    response = requests.get(
        f"{server}/{values['path']}?z={values['query']}", allow_redirects=False, timeout=5
    )
    assert response.status_code == 302
    _assert_clean(capfd.readouterr().out, values)


@pytest.mark.parametrize("level", _ROOT_LEVELS)
def test_malformed_response_header_logs_no_path_query_or_cookie(
    level: int, server: str, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level)
    response = requests.get(
        f"{server}/broken/{values['path']}?z={values['query']}", allow_redirects=False, timeout=5
    )
    assert response.status_code == 200
    _assert_clean(capfd.readouterr().out, values)


@pytest.mark.parametrize("level", _ROOT_LEVELS)
def test_retry_after_broken_connection_logs_no_path_or_query(
    level: int, closing_server: str, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level)
    with requests.Session() as session:
        session.mount("http://", HTTPAdapter(max_retries=1))
        with pytest.raises(requests.ConnectionError):
            session.get(f"{closing_server}/{values['path']}?z={values['query']}", timeout=5)
    _assert_clean(capfd.readouterr().out, values)


@pytest.mark.parametrize("level", _ROOT_LEVELS)
def test_real_httpx2_call_logs_no_query_or_cookie(
    level: int, server: str, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level)
    with httpx2.Client(follow_redirects=False) as client:
        response = client.get(f"{server}/{values['path']}?z={values['query']}")
    assert response.status_code == 302
    _assert_clean(capfd.readouterr().out, values)


@pytest.mark.parametrize("level", _ROOT_LEVELS)
def test_real_ai_sdk_calls_log_no_prompt_or_header(
    level: int, server: str, values: dict[str, str], capfd: pytest.CaptureFixture[str]
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
    _assert_clean(capfd.readouterr().out, values)


@pytest.mark.parametrize("sdk_log", ["debug", "info"])
def test_anthropic_imported_after_setup_logs_no_prompt(
    sdk_log: str, server: str, values: dict[str, str]
) -> None:
    script = textwrap.dedent(
        """
        import logging, sys
        from meho_backplane.logging import configure_logging
        configure_logging(logging.INFO)
        import anthropic
        server, query, prompt, sdk_level = sys.argv[1:5]
        assert logging.getLogger("anthropic").level == getattr(logging, sdk_level)
        with anthropic.Anthropic(base_url=server, api_key="test-key", max_retries=0) as c:
            c.messages.create(
                model="m", max_tokens=8,
                messages=[{"role": "user", "content": "say " + prompt}],
                extra_query={"z": query},
            )
        logging.getLogger("meho.test").info("done")
        """
    )
    env = {**os.environ, "ANTHROPIC_LOG": sdk_log}
    done = subprocess.run(
        [sys.executable, "-c", script, server, values["query"], values["prompt"], sdk_log.upper()],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert any(json.loads(line).get("event") == "done" for line in done.stdout.splitlines())
    _assert_clean(done.stdout + done.stderr, values)


@pytest.mark.parametrize("name", _STACK_LOGGERS)
@pytest.mark.parametrize("root_level", [logging.NOTSET, logging.DEBUG])
def test_stack_logger_line_below_held_level_is_dropped(
    name: str, root_level: int, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(root_level)
    logger = logging.getLogger(name)
    held = _held_for(name)
    # A logger of a fixed line (a warning MEHO writes as its own line) makes its
    # WARNING records, so the handler filter can judge them.
    safe = any(name == n or name.startswith(f"{n}.") for n, _ in _SAFE_LINES)
    assert logger.getEffectiveLevel() == (min(held, logging.WARNING) if safe else held)
    for level in range(logging.DEBUG, held, 10):
        logger.log(level, "GET /%s?k=%s", values["path"], values["query"])
    _assert_clean(capfd.readouterr().out, values)


@pytest.mark.parametrize("name", _STACK_LOGGERS)
def test_stack_logger_level_lowered_after_setup_is_still_held(
    name: str, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(logging.INFO)
    held = _held_for(name)
    logging.getLogger(name).setLevel(logging.DEBUG)
    logging.getLogger(name.split(".")[0]).setLevel(logging.DEBUG)
    logger = logging.getLogger(name)
    for level in range(logging.DEBUG, held, 10):
        logger.log(level, "GET /%s?k=%s", values["path"], values["query"])
    logger.log(held, "held-level line %s", values["header"])
    out = capfd.readouterr().out
    assert values["path"] not in out
    assert values["query"] not in out
    assert values["header"] in out
