# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the fixed log lines of four HTTP library warnings and for the hold
of every other record of an HTTP library below its held level, in every
handler."""

from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from requests.adapters import HTTPAdapter

import meho_backplane.logging as meho_logging
from meho_backplane.logging import configure_logging

_ROOT_LEVELS = [logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING]
_LOGGERS = (
    "",
    "urllib3",
    "urllib3.connection",
    "urllib3.connectionpool",
    "botocore",
    "botocore.credentials",
    "botocore.tokens",
    "aiohttp.client",
)

# One entry for each fixed line: logger, the library's format text, the event.
_KINDS = [
    (
        "urllib3.connection",
        "Certificate did not match expected hostname: %s. Certificate: %s",
        "http_tls_name_mismatch",
    ),
    (
        "urllib3.connectionpool",
        "Retrying (%r) after connection broken by '%r': %s",
        "http_retry_after_broken_connection",
    ),
    (
        "botocore.credentials",
        "Refreshing temporary credentials failed during %s refresh period.",
        "aws_credential_refresh_failed",
    ),
    (
        "botocore.tokens",
        "Refreshing token failed during the %s refresh period.",
        "aws_token_refresh_failed",
    ),
    ("botocore.tokens", "SSO token refresh attempt failed", "aws_token_refresh_failed"),
]


def _state() -> dict[str, tuple[int, bool]]:
    return {n: (logging.getLogger(n).level, logging.getLogger(n).disabled) for n in _LOGGERS}


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    factory = logging.getLogRecordFactory()
    previous = meho_logging._previous_record_factory
    saved = _state()
    for n in _LOGGERS:
        logging.getLogger(n).disabled = False
    root_handlers = list(logging.getLogger().handlers)
    try:
        yield
    finally:
        logging.setLogRecordFactory(factory)
        meho_logging._previous_record_factory = previous
        for n, (lvl, disabled) in saved.items():
            logging.getLogger(n).setLevel(lvl)
            logging.getLogger(n).disabled = disabled
        for n in _LOGGERS:
            for h in [h for h in logging.getLogger(n).handlers if h not in root_handlers]:
                if n or h.name != meho_logging._STDLIB_BRIDGE_HANDLER_NAME:
                    logging.getLogger(n).removeHandler(h)


@pytest.fixture
def values() -> dict[str, str]:
    return {k: f"{k[0]}{secrets.token_hex(6)}" for k in ("addr", "cert", "exc", "path", "query")}


def _events(out: str) -> list[str]:
    return [json.loads(line)["event"] for line in out.splitlines() if line.startswith("{")]


def _assert_clean(out: str, values: dict[str, str]) -> None:
    for value in values.values():
        assert value not in out


def _send(name: str, msg: str, values: dict[str, str]) -> None:
    try:
        raise ValueError(values["exc"])
    except ValueError:
        logging.getLogger(name).warning(
            msg,
            *([values["addr"], values["cert"], values["query"]][: msg.count("%")]),
            exc_info=True,
        )


@pytest.mark.parametrize("level", _ROOT_LEVELS)
@pytest.mark.parametrize(("name", "msg", "event"), _KINDS)
def test_each_kind_writes_its_fixed_line_once(
    name: str,
    msg: str,
    event: str,
    level: int,
    values: dict[str, str],
    capfd: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level)
    _send(name, msg, values)
    out = capfd.readouterr().out
    events = _events(out)
    assert len([e for e in events if e.startswith(f"{event}:")]) == 1
    assert len(events) == 1
    assert json.loads(out)["level"] == "warning"
    assert "exception" not in json.loads(out)
    _assert_clean(out, values)


@pytest.fixture
def closing_server() -> Iterator[str]:
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


@pytest.mark.parametrize("level", _ROOT_LEVELS)
def test_real_retry_writes_the_fixed_line_once(
    level: int,
    closing_server: str,
    values: dict[str, str],
    capfd: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level)
    with requests.Session() as session:
        session.mount("http://", HTTPAdapter(max_retries=1))
        with pytest.raises(requests.ConnectionError):
            session.get(f"{closing_server}/{values['path']}?z={values['query']}", timeout=5)
    out = capfd.readouterr().out
    assert len([e for e in _events(out) if e.startswith("http_retry_after_broken_connection")]) == 1
    _assert_clean(out, values)


@pytest.mark.parametrize("level", _ROOT_LEVELS)
@pytest.mark.parametrize("name", ["urllib3.connectionpool", "botocore", "aiohttp.client"])
def test_other_warning_of_a_held_logger_is_dropped(
    name: str, level: int, values: dict[str, str], capfd: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level)
    logging.getLogger(name).warning("Other text %s %s", values["addr"], values["query"])
    logging.getLogger(name).warning("Could not parse .netrc file: %s", values["cert"])
    assert capfd.readouterr().out == ""


def test_every_handler_gets_only_fixed_text(
    values: dict[str, str], capfd: pytest.CaptureFixture[str], closing_server: str
) -> None:
    configure_logging(logging.NOTSET)
    seen: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(self.format(record))

    root_handler, lib_handler = Collect(), Collect()
    for h in (root_handler, lib_handler):
        h.setFormatter(logging.Formatter("%(name)s %(message)s %(exc_text)s"))
    logging.getLogger().addHandler(root_handler)
    library = logging.getLogger("urllib3")
    library.addHandler(lib_handler)
    library.setLevel(logging.DEBUG)
    library.debug("GET /%s?k=%s", values["path"], values["query"])
    _send("urllib3.connection", "Failed to parse headers (url=%s): %s", values)
    with requests.Session() as session:
        session.mount("http://", HTTPAdapter(max_retries=1))
        with pytest.raises(requests.ConnectionError):
            session.get(f"{closing_server}/{values['path']}?z={values['query']}", timeout=5)
    assert seen
    _assert_clean("\n".join(seen) + capfd.readouterr().out, values)


def test_hold_record_factory_is_set_once() -> None:
    configure_logging(logging.INFO)
    configure_logging(logging.INFO)
    assert logging.getLogRecordFactory() is meho_logging._hold_record_factory
    other: Any = meho_logging._previous_record_factory
    assert other is not meho_logging._hold_record_factory
