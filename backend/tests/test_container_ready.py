# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the wait-until-ready helper of the test containers (tests/_container_ready.py)."""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import bson
import pytest

from tests._container_ready import (
    ContainerNotReadyError,
    ContainerStartRetryWarning,
    ping_mongodb,
    start_until_ready,
    wait_until_ready,
)


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


# A made-up text in the container's log and in its errors. It must never
# reach the test output: the CI logs of this repository are public.
_MARKER = "zz-marker-7f3a"


class _StandInContainer:
    """A started container that answers or not, and records whether it was stopped."""

    def __init__(self, name: str, *, ready: bool) -> None:
        self.name = name
        self.ready = ready
        self.stopped = False

    def get_logs(self) -> tuple[bytes, bytes]:
        return f"{_MARKER} stdout\n".encode(), f"{_MARKER} stderr\n".encode()

    def stop(self) -> None:
        self.stopped = True


def _starter(
    *tries: bool | BaseException,
) -> tuple[list[_StandInContainer], Callable[[], _StandInContainer]]:
    """Each try is ``True`` (ready), ``False`` (no answer) or an error that ``start()`` raises."""
    started: list[_StandInContainer] = []
    calls: list[int] = []

    def start() -> _StandInContainer:
        outcome = tries[len(calls)]
        calls.append(1)
        if isinstance(outcome, BaseException):
            raise outcome
        container = _StandInContainer(f"try{len(calls)}", ready=outcome)
        started.append(container)
        return container

    return started, start


def _probe(container: _StandInContainer) -> None:
    if not container.ready:
        raise ConnectionRefusedError(f"connection refused {_MARKER}")


def test_start_until_ready_starts_once_when_the_first_container_answers() -> None:
    fake = _FakeClock()
    started, start = _starter(True)

    with warnings.catch_warnings():
        warnings.simplefilter("error", ContainerStartRetryWarning)
        container = start_until_ready(
            start, _probe, what="mongodb", clock=fake.clock, sleep=fake.sleep
        )

    assert container is started[0]
    assert len(started) == 1
    assert not container.stopped


def test_start_until_ready_starts_a_fresh_container_when_the_first_gives_no_answer() -> None:
    fake = _FakeClock()
    started, start = _starter(False, True)

    with pytest.warns(ContainerStartRetryWarning) as record:
        container = start_until_ready(
            start,
            _probe,
            what="mongodb (mongo-7, image mongo:7)",
            timeout=60,
            second_timeout=120,
            interval=1,
            clock=fake.clock,
            sleep=fake.sleep,
        )

    assert container is started[1]
    assert started[0].stopped
    assert not container.stopped
    assert len(record) == 1
    assert str(record[0].message) == (
        "container not ready: mongodb (mongo-7, image mongo:7) gave no answer in 60 s "
        "(ContainerNotReadyError), started a fresh container once more"
    )


def test_start_until_ready_starts_again_when_start_itself_times_out() -> None:
    fake = _FakeClock()
    started, start = _starter(TimeoutError(_MARKER), True)

    with pytest.warns(ContainerStartRetryWarning) as record:
        container = start_until_ready(
            start, _probe, what="mongodb", interval=1, clock=fake.clock, sleep=fake.sleep
        )

    assert started == [container]
    assert not container.stopped
    assert len(record) == 1
    assert "(TimeoutError), started a fresh container once more" in str(record[0].message)
    assert _MARKER not in str(record[0].message)


def test_start_until_ready_lets_another_start_error_leave_at_once() -> None:
    fake = _FakeClock()
    calls: list[int] = []

    def start() -> _StandInContainer:
        calls.append(1)
        raise OSError("image pull failed")

    with warnings.catch_warnings():
        warnings.simplefilter("error", ContainerStartRetryWarning)
        with pytest.raises(OSError, match="image pull failed"):
            start_until_ready(start, _probe, what="x", clock=fake.clock, sleep=fake.sleep)

    assert len(calls) == 1


@pytest.mark.parametrize(
    ("tries", "kinds"),
    [
        ((False, False), ("ContainerNotReadyError", "ContainerNotReadyError")),
        ((False, TimeoutError(_MARKER)), ("ContainerNotReadyError", "TimeoutError")),
        ((TimeoutError(_MARKER), False), ("TimeoutError", "ContainerNotReadyError")),
    ],
)
def test_start_until_ready_names_both_tries_when_the_second_start_fails_too(
    tries: tuple[bool | BaseException, ...], kinds: tuple[str, str]
) -> None:
    fake = _FakeClock()
    started, start = _starter(*tries)

    with (
        pytest.warns(ContainerStartRetryWarning),
        pytest.raises(ContainerNotReadyError) as excinfo,
    ):
        start_until_ready(
            start,
            _probe,
            what="mongodb (mongo-7, image mongo:7)",
            timeout=60,
            second_timeout=120,
            interval=1,
            clock=fake.clock,
            sleep=fake.sleep,
        )

    assert str(excinfo.value) == (
        "container not ready: mongodb (mongo-7, image mongo:7) failed on both tries\n"
        f"  try 1: no answer in 60 s ({kinds[0]})\n"
        f"  try 2: no answer in 120 s ({kinds[1]})"
    )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert all(container.stopped for container in started)


def test_start_until_ready_stops_the_container_on_an_unexpected_error() -> None:
    fake = _FakeClock()
    started, start = _starter(True)

    def probe(container: _StandInContainer) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        start_until_ready(start, probe, what="x", clock=fake.clock, sleep=fake.sleep)

    assert started[0].stopped


_INNER_TEST = """
import pytest

from tests._container_ready import start_until_ready
from tests.test_container_ready import _FakeClock, _MARKER, _probe, _starter


def test_uses_a_container_that_never_answers():
    fake = _FakeClock()
    _, start = _starter(False, TimeoutError(_MARKER))
    start_until_ready(start, _probe, what="mongodb", clock=fake.clock, sleep=fake.sleep)
"""


def test_failure_output_holds_no_container_log_or_error_text(tmp_path: Path) -> None:
    """The marker in the container's log and errors is in no output line and no junit file."""
    (tmp_path / "test_inner.py").write_text(_INNER_TEST)
    junit = tmp_path / "junit.xml"
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            os.devnull,
            "--rootdir",
            str(tmp_path),
            "-rA",
            f"--junitxml={junit}",
            str(tmp_path / "test_inner.py"),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(backend)},
        timeout=120,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "failed on both tries" in output
    assert "try 1: no answer in 60 s (ContainerNotReadyError)" in output
    assert "try 2: no answer in 120 s (TimeoutError)" in output
    assert _MARKER not in output
    assert _MARKER not in junit.read_text()


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
            # The client closed the connection; this fake server just stops serving it.
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
