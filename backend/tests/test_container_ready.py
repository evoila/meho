# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the wait-until-ready helper of the test containers (tests/_container_ready.py)."""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Iterator

import bson
import pytest

from tests._container_ready import ContainerNotReadyError, ping_mongodb, wait_until_ready


class _FakeClock:
    """Time that moves only when ``sleep`` is called, so no test waits for real."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _connect(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
        pass


def test_returns_at_once_when_the_probe_works() -> None:
    fake = _FakeClock()
    calls: list[int] = []

    wait_until_ready(lambda: calls.append(1), what="x", clock=fake.clock, sleep=fake.sleep)

    assert calls == [1]
    assert fake.now == 0.0


def test_waits_and_passes_when_the_probe_fails_first() -> None:
    fake = _FakeClock()
    attempts = {"n": 0}

    def probe() -> None:
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise ConnectionRefusedError("connection refused")

    wait_until_ready(probe, what="x", timeout=30, interval=1, clock=fake.clock, sleep=fake.sleep)

    assert attempts["n"] == 4
    assert fake.now == 3.0


def test_fails_with_a_clear_message_when_the_probe_never_works() -> None:
    fake = _FakeClock()

    def probe() -> None:
        raise ConnectionRefusedError("connection refused")

    with pytest.raises(ContainerNotReadyError) as excinfo:
        wait_until_ready(
            probe, what="mongodb", timeout=10, interval=1, clock=fake.clock, sleep=fake.sleep
        )

    message = str(excinfo.value)
    assert message.startswith("container not ready: mongodb gave no answer in 10 s")
    assert "ConnectionRefusedError" in message
    assert fake.now <= 11.0


def test_real_port_that_opens_late_is_waited_for() -> None:
    """A port that refuses first and opens after about 0.5 s: the wait ends the failure."""
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    ready: list[socket.socket] = []

    def open_late() -> None:
        time.sleep(0.5)
        server = socket.socket()
        server.bind(("127.0.0.1", port))
        server.listen()
        ready.append(server)

    with pytest.raises(ConnectionRefusedError):
        _connect(port)
    thread = threading.Thread(target=open_late)
    thread.start()
    try:
        wait_until_ready(lambda: _connect(port), what="late port", timeout=10, interval=0.1)
    finally:
        thread.join()
        for server in ready:
            server.close()


def test_real_port_that_never_opens_fails_inside_the_time_limit() -> None:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]

    started = time.monotonic()
    with pytest.raises(ContainerNotReadyError, match="container not ready: closed port"):
        wait_until_ready(lambda: _connect(port), what="closed port", timeout=1, interval=0.1)

    assert time.monotonic() - started < 3


_OP_REPLY = 1
_OP_QUERY = 2004
_OP_MSG = 2013
_HELLO_REPLY = {
    "ok": 1.0,
    "isWritablePrimary": True,
    "ismaster": True,
    "minWireVersion": 0,
    "maxWireVersion": 21,
    "maxBsonObjectSize": 16 * 1024 * 1024,
    "maxMessageSizeBytes": 48_000_000,
    "maxWriteBatchSize": 100_000,
}


def _read_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("client closed")
        data += chunk
    return data


def _command(op_code: int, body: bytes) -> dict[str, object]:
    if op_code == _OP_QUERY:
        start = body.index(b"\0", 4) + 1 + 8  # flags, collection name, skip, limit
        return dict(bson.decode(body[start:]))
    return dict(bson.decode(body[5:]))  # OP_MSG: flags, section kind 0


def _reply(request_id: int, op_code: int) -> bytes:
    doc = bson.encode(_HELLO_REPLY)
    if op_code == _OP_QUERY:
        payload = struct.pack("<iqii", 0, 0, 0, 1) + doc
        reply_code = _OP_REPLY
    else:
        payload = struct.pack("<I", 0) + b"\0" + doc
        reply_code = _OP_MSG
    return struct.pack("<iiii", 16 + len(payload), 0, request_id, reply_code) + payload


def _answer_hello_only(conn: socket.socket, stop: threading.Event) -> None:
    """Answer the hello hand-shake, then never answer anything else."""
    with conn:
        try:
            while not stop.is_set():
                length, request_id, _, op_code = struct.unpack("<iiii", _read_exact(conn, 16))
                command = _command(op_code, _read_exact(conn, length - 16))
                if {"hello", "isMaster", "ismaster"} & command.keys():
                    conn.sendall(_reply(request_id, op_code))
                else:
                    stop.wait()
        except OSError:
            pass


@pytest.fixture
def silent_mongo_port() -> Iterator[int]:
    """A port that takes connections and says hello like MongoDB, then stays silent."""
    stop = threading.Event()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.1)
    threads: list[threading.Thread] = []

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            thread = threading.Thread(target=_answer_hello_only, args=(conn, stop), daemon=True)
            thread.start()
            threads.append(thread)

    acceptor = threading.Thread(target=serve, daemon=True)
    acceptor.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        acceptor.join()
        server.close()
        for thread in threads:
            thread.join(timeout=2)


def test_mongodb_ping_ends_when_the_server_connects_but_never_answers(
    silent_mongo_port: int,
) -> None:
    """The server is found and takes the connection, but the ping gets no answer."""
    started = time.monotonic()
    with pytest.raises(ContainerNotReadyError, match="container not ready: silent mongodb"):
        wait_until_ready(
            lambda: ping_mongodb(
                "127.0.0.1", silent_mongo_port, username="u", password="p", timeout_ms=300
            ),
            what="silent mongodb",
            timeout=1,
            interval=0.1,
        )

    assert time.monotonic() - started < 5
