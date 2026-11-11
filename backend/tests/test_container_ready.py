# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the wait-until-ready helper of the test containers (tests/_container_ready.py)."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from tests._container_ready import ContainerNotReadyError, wait_until_ready


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
