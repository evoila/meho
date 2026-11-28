# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Wait until a test container really answers, before a test uses it.

A container can be "started" before its service takes connections. The
MongoDB image is the known case: its start-up script first runs a
short-lived server that only listens on a local socket, prints the same
"waiting for connections" line, then restarts the real server. A test
that connects right after the log wait gets "connection refused".

``wait_until_ready`` calls a probe again and again until it returns, or
until the time limit ends. On timeout it raises ``ContainerNotReadyError``.
The wait checks its time limit only between two probe calls, so each
probe call must have a short time limit of its own (``ping_mongodb`` sets
one for the whole round trip).
The message starts with ``container not ready`` so a person (or the
fleet's loop) can tell a set-up failure from a failing test.

Usage::

    wait_until_ready(
        lambda: ping_mongodb(host, port, username=user, password=password),
        what="mongodb",
        timeout=60,
    )
"""

from __future__ import annotations

import time
from collections.abc import Callable


class ContainerNotReadyError(RuntimeError):
    """A test container did not answer inside its time limit."""


def wait_until_ready(
    probe: Callable[[], object],
    *,
    what: str,
    timeout: float = 60.0,
    interval: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Call ``probe`` until it returns without raising, or fail after ``timeout`` seconds.

    ``what`` names the container in the error message. ``clock`` and
    ``sleep`` can be replaced so a test does not wait in real time.
    """
    deadline = clock() + timeout
    last_error: Exception | None = None
    while True:
        try:
            probe()
            return
        except Exception as exc:  # any probe error means "not ready yet"
            last_error = exc
        if clock() >= deadline:
            break
        sleep(interval)
    raise ContainerNotReadyError(
        f"container not ready: {what} gave no answer in {timeout:g} s "
        f"(last error: {type(last_error).__name__}: {last_error})"
    )


def ping_mongodb(
    host: str, port: int, *, username: str, password: str, timeout_ms: int = 2000
) -> None:
    """One real ``ping`` round trip to MongoDB, never longer than ``timeout_ms``.

    ``timeoutMS`` limits the whole call: finding the server, logging in and
    waiting for the answer. ``serverSelectionTimeoutMS`` alone would not
    stop a server that takes the connection but never answers.
    """
    from pymongo import MongoClient

    client: MongoClient[dict[str, object]] = MongoClient(
        host=host,
        port=port,
        username=username,
        password=password,
        authSource="admin",
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        timeoutMS=timeout_ms,
    )
    try:
        client.admin.command("ping")
    finally:
        client.close()
