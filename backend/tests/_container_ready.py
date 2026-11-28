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

``start_test_container`` is the one way an integration fixture starts a
container. It makes a fresh container with ``make``, starts it through
``start_until_ready`` and returns it. When the container does not start,
it fails the test under CI (``GITHUB_ACTIONS`` set) and skips it on a
laptop. Its message names the image without its registry part and the
kind of error (a fixed word and the error's class name). It holds no
text of the Docker daemon, and the failure has no traceback. A registry
that refuses the pull for too many requests gets the same answer on the
first and on the second try.

``start_until_ready`` starts a container and waits for it. When the
container gives no answer in time, it stops it and starts a fresh one
once, with a longer wait. A second start shows as one
``ContainerStartRetryWarning`` line in the pytest warnings summary, so
we can count how often it happens. A ``TimeoutError`` raised by ``start``
itself (testcontainers waits for a log line inside ``start()``) counts as
"no answer in time" too; every other error of the first start leaves at
once. When the second start fails too, for any reason, the error names
both tries with the wait time and the kind of error only. It holds no container log line and no
error message: the CI logs of this repository are public.

Usage::

    container = start_test_container(
        lambda: MongoDbContainer(image),  # makes a fresh container, not started
        image=image,
        what="mongodb",
        probe=lambda c: ping_mongodb(c.host, c.port, username=user, password=password),
    )
"""

from __future__ import annotations

import contextlib
import os
import time
import warnings
from collections.abc import Callable
from typing import NoReturn, Protocol

import pytest


class ContainerNotReadyError(RuntimeError):
    """A test container did not answer inside its time limit."""


class ContainerStartRetryWarning(UserWarning):
    """A test container gave no answer in time and was started a second time."""


class StartedContainer(Protocol):
    """The parts of a started testcontainers container that the helper uses."""

    def stop(self) -> None: ...


class StartableContainer(StartedContainer, Protocol):
    """A testcontainers container that is made but not started yet."""

    def start(self) -> object: ...


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
        f"(last error: {type(last_error).__name__})"
    )


def start_until_ready[C: StartedContainer](
    start: Callable[[], C],
    probe: Callable[[C], object],
    *,
    what: str,
    timeout: float = 60.0,
    second_timeout: float = 120.0,
    interval: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> C:
    """Start a container and wait until ``probe`` works; on a timeout start a fresh one once.

    The first container is stopped before the second start. A container
    that is not returned is always stopped. ``start`` must stop its own
    container when its ``start()`` raises.
    """
    first = _try_start(start, probe, what, timeout, interval, clock, sleep, TimeoutError)
    if not isinstance(first, Exception):
        return first

    warnings.warn(
        f"container not ready: {what} gave no answer in {timeout:g} s "
        f"({type(first).__name__}), started a fresh container once more",
        ContainerStartRetryWarning,
        stacklevel=2,
    )
    second = _try_start(start, probe, what, second_timeout, interval, clock, sleep, Exception)
    if not isinstance(second, Exception):
        return second
    if _pull_refused_for_too_many_requests(second):
        # The same answer as a refused pull on the first try.
        raise second
    if isinstance(second, ContainerNotReadyError | TimeoutError):
        try_2 = f"no answer in {second_timeout:g} s"
    else:
        try_2 = "start failed"
    raise ContainerNotReadyError(
        f"container not ready: {what} failed on both tries\n"
        f"  try 1: no answer in {timeout:g} s ({type(first).__name__})\n"
        f"  try 2: {try_2} ({type(second).__name__})"
    )


def _try_start[C: StartedContainer](
    start: Callable[[], C],
    probe: Callable[[C], object],
    what: str,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    start_errors: type[Exception],
) -> C | Exception:
    """One try: the ready container, or the error of this try.

    An error of ``start`` that is a ``start_errors`` is returned; any other leaves.
    """
    try:
        container = start()
    except start_errors as exc:
        return exc
    try:
        wait_until_ready(
            lambda: probe(container),
            what=what,
            timeout=timeout,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
    except ContainerNotReadyError as exc:
        _stop_quietly(container)
        return exc
    except BaseException:
        _stop_quietly(container)
        raise
    return container


def start_test_container[C: StartableContainer](
    make: Callable[[], C],
    *,
    image: str,
    what: str,
    probe: Callable[[C], object] | None = None,
    timeout: float = 60.0,
    second_timeout: float = 120.0,
    interval: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> C:
    """Start a fresh container from ``make`` until ``probe`` works, or end the test.

    ``make`` returns a container that is not started yet; it is called
    again for the second try. ``probe`` gets the started container; with
    no probe, a container whose ``start()`` returns is ready. ``image``
    and ``what`` name the container in the message. The caller stops the
    returned container.
    """

    def start() -> C:
        container = make()
        try:
            container.start()
        except BaseException:
            # A start that fails can leave a running container behind.
            _stop_quietly(container)
            raise
        return container

    label = f"{what} (image {_image_without_registry(image)})"
    try:
        return start_until_ready(
            start,
            probe if probe is not None else _no_probe,
            what=label,
            timeout=timeout,
            second_timeout=second_timeout,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
    except Exception as exc:
        message = container_start_message(label, exc)
    _end_test(message)


def run_docker_step[T](
    step: Callable[[], T], *, what: str, failed: str, image: str | None = None
) -> T:
    """Run a Docker step before a container start (an image build, a network) the same way.

    When ``step`` raises, the test ends as in ``start_test_container``: it
    fails under CI and skips on a laptop. ``failed`` names the step in the
    message, for example ``"image build failed"``.
    """
    label = what if image is None else f"{what} (image {_image_without_registry(image)})"
    try:
        return step()
    except Exception as exc:
        message = container_start_message(label, exc, failed=failed)
    _end_test(message)


def container_start_message(
    label: str, error: BaseException, *, failed: str = "start failed"
) -> str:
    """The fixed message for a container that did not start: never the error's own text."""
    if isinstance(error, ContainerNotReadyError):
        # Built only from fixed words, wait times and class names.
        return str(error)
    kind = type(error).__name__
    if _pull_refused_for_too_many_requests(error):
        return (
            f"container did not start: {label}: "
            f"the registry refused the pull, too many requests ({kind})"
        )
    return f"container did not start: {label}: {failed} ({kind})"


def _end_test(message: str) -> NoReturn:
    # Called out of the ``except`` block, so the error is not chained to the failure.
    if os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _pull_refused_for_too_many_requests(error: BaseException) -> bool:
    # The text is read to sort the error, never printed.
    if getattr(error, "status_code", None) == 429:
        return True
    text = str(error).lower()
    return "too many requests" in text or "rate limit" in text


def _image_without_registry(image: str) -> str:
    # The registry part can name a private mirror; keep only the image's own name.
    return image.rsplit("/", 1)[-1]


def _no_probe(container: object) -> None:
    return None


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
