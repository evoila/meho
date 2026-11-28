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

``start_until_ready`` starts a container and waits for it. When the
container gives no answer in time, it stops it and starts a fresh one
once, with a longer wait. A second start shows as one
``ContainerStartRetryWarning`` line in the pytest warnings summary, so
we can count how often it happens. When the second start fails too, the
error names both tries and the last log lines of the container.

Usage::

    container = start_until_ready(
        start_mongo,  # makes and starts a fresh container
        lambda c: ping_mongodb(c.host, c.port, username=user, password=password),
        what="mongodb",
    )
"""

from __future__ import annotations

import contextlib
import time
import warnings
from collections.abc import Callable
from typing import Protocol


class ContainerNotReadyError(RuntimeError):
    """A test container did not answer inside its time limit."""


class ContainerStartRetryWarning(UserWarning):
    """A test container gave no answer in time and was started a second time."""


class StartedContainer(Protocol):
    """The parts of a started testcontainers container that the helper uses."""

    def get_logs(self) -> tuple[bytes, bytes]: ...

    def stop(self) -> None: ...


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


def start_until_ready[C: StartedContainer](
    start: Callable[[], C],
    probe: Callable[[C], object],
    *,
    what: str,
    timeout: float = 60.0,
    second_timeout: float = 120.0,
    interval: float = 0.5,
    log_lines: int = 20,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> C:
    """Start a container and wait until ``probe`` works; on a timeout start a fresh one once.

    The first container is stopped before the second start. A container
    that is not returned is always stopped.
    """
    first = start()
    try:
        wait_until_ready(
            lambda: probe(first),
            what=what,
            timeout=timeout,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
        return first
    except ContainerNotReadyError as exc:
        first_error = exc
        _stop_quietly(first)
    except BaseException:
        _stop_quietly(first)
        raise

    warnings.warn(
        f"container not ready: {what} gave no answer in {timeout:g} s, "
        "started a fresh container once more",
        ContainerStartRetryWarning,
        stacklevel=2,
    )
    second = start()
    try:
        wait_until_ready(
            lambda: probe(second),
            what=what,
            timeout=second_timeout,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
        return second
    except ContainerNotReadyError as exc:
        tail = _log_tail(second, log_lines)
        _stop_quietly(second)
        raise ContainerNotReadyError(
            f"container not ready: {what} failed on both tries\n"
            f"  try 1: {first_error}\n"
            f"  try 2: {exc}\n"
            f"  last {log_lines} log lines of try 2:\n{tail}"
        ) from exc
    except BaseException:
        _stop_quietly(second)
        raise


def _log_tail(container: StartedContainer, lines: int) -> str:
    try:
        stdout, stderr = container.get_logs()
    except Exception as exc:  # the logs only help the message; never hide the real error
        return f"    (no logs: {type(exc).__name__}: {exc})"
    text = (stdout + stderr).decode("utf-8", errors="replace")
    return "\n".join(f"    {line}" for line in text.splitlines()[-lines:])


def _stop_quietly(container: StartedContainer) -> None:
    # A failed stop must not hide why the container was stopped.
    with contextlib.suppress(Exception):
        container.stop()


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
