# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Limits on work that one caller can start: probes, imports and login flows."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import tracemalloc
import uuid
from typing import Any, ClassVar

import pytest
import yaml
from pydantic import ValidationError

from meho_backplane.connectors.net import tls as net_tls
from meho_backplane.connectors.net.probe_pool import (
    PROBE_POOL_WORKERS,
    PROBE_TENANT_SHARE,
    ProbePoolBusyError,
    run_blocking_probe,
)
from meho_backplane.operations._errors import is_probe_pool_busy, result_probe_pool_busy
from meho_backplane.targets.extras_limits import (
    EXTRAS_MAX_DEPTH,
    EXTRAS_MAX_NODES,
    check_extras_size,
)
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.ui.auth import routes as auth_routes
from meho_backplane.ui.auth.verifier_store import MAX_PENDING_FLOWS, PKCEVerifierStore
from meho_backplane.ui.routes.connectors.import_view import ImportParseError, _parse_targets_yaml
from meho_backplane.ui.routes.topology.batch_parse import BulkParseError, parse_bulk_rows
from meho_backplane.ui.safe_yaml import load_yaml_no_alias
from tests._time_limits import check_time_limit

_ALIAS_BOMB = """
a: &a [x, x, x, x, x, x, x, x, x, x]
b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]
c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]
d: [*c, *c, *c, *c, *c, *c, *c, *c, *c, *c]
"""


# --- probe pool ------------------------------------------------------------


def _tenants(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


async def _wait_until(check: Any) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def _fill_pool(silent_peer: Any) -> list[asyncio.Task[Any]]:
    """Fill every probe thread, each tenant with its full share."""
    tenants = _tenants(PROBE_POOL_WORKERS // PROBE_TENANT_SHARE)
    return [
        asyncio.create_task(run_blocking_probe(silent_peer, tenant_id=tenant, timeout=10))
        for tenant in tenants
        for _ in range(PROBE_TENANT_SHARE)
    ]


def test_tenant_share_is_below_the_number_of_threads() -> None:
    assert PROBE_TENANT_SHARE < PROBE_POOL_WORKERS


@pytest.mark.slow_runner_tolerant
async def test_waiting_probes_leave_the_default_thread_pool_free() -> None:
    release = threading.Event()
    running: list[int] = []

    def _silent_peer() -> str:
        running.append(1)
        release.wait(10)
        return "done"

    probes = _fill_pool(_silent_peer)
    # One more probe than the pool has threads: it waits in the probe queue.
    queued = asyncio.create_task(
        run_blocking_probe(_silent_peer, tenant_id=uuid.uuid4(), timeout=10)
    )
    try:
        await _wait_until(lambda: len(running) == PROBE_POOL_WORKERS)
        begin = time.monotonic()
        assert await asyncio.to_thread(lambda: "login ok") == "login ok"
        # A blocked pool would hold this call for the full 10 s.
        waited = time.monotonic() - begin
        check_time_limit(waited, 2.0, f"the default thread pool held the call {waited:.2f} s")
        assert not queued.done()
    finally:
        release.set()
    assert await asyncio.gather(*probes, queued) == ["done"] * (PROBE_POOL_WORKERS + 1)


async def test_tenant_over_its_share_waits_and_is_refused_after_its_timeout() -> None:
    release = threading.Event()
    running: list[str] = []

    def _silent_peer(name: str) -> str:
        running.append(name)
        release.wait(10)
        return name

    busy_tenant, other_tenant = _tenants(2)
    held = [
        asyncio.create_task(
            run_blocking_probe(_silent_peer, "busy", tenant_id=busy_tenant, timeout=10)
        )
        for _ in range(PROBE_TENANT_SHARE)
    ]
    try:
        await _wait_until(lambda: len(running) == PROBE_TENANT_SHARE)
        begin = time.monotonic()
        with pytest.raises(ProbePoolBusyError, match="probe_pool_busy"):
            await run_blocking_probe(_silent_peer, "refused", tenant_id=busy_tenant, timeout=0.2)
        assert time.monotonic() - begin >= 0.2
        other = asyncio.create_task(
            run_blocking_probe(_silent_peer, "other", tenant_id=other_tenant, timeout=10)
        )
        await _wait_until(lambda: "other" in running)
    finally:
        release.set()
    assert await other == "other"
    assert await asyncio.gather(*held) == ["busy"] * PROBE_TENANT_SHARE
    assert "refused" not in running


async def test_probes_over_the_share_wait_for_it_and_all_run() -> None:
    running: list[int] = []
    most_at_once: list[int] = []

    def _short_probe() -> str:
        running.append(1)
        most_at_once.append(len(running))
        time.sleep(0.05)
        running.pop()
        return "ok"

    tenant = uuid.uuid4()
    results = await asyncio.gather(
        *(
            run_blocking_probe(_short_probe, tenant_id=tenant, timeout=5)
            for _ in range(PROBE_TENANT_SHARE + 1)
        )
    )
    assert results == ["ok"] * (PROBE_TENANT_SHARE + 1)
    assert max(most_at_once) <= PROBE_TENANT_SHARE


async def test_cancelled_waiting_probe_gives_up_its_place_in_line() -> None:
    release = threading.Event()
    running: list[str] = []

    def _silent_peer(name: str) -> str:
        running.append(name)
        release.wait(10)
        return name

    tenant = uuid.uuid4()
    held = [
        asyncio.create_task(run_blocking_probe(_silent_peer, "held", tenant_id=tenant, timeout=10))
        for _ in range(PROBE_TENANT_SHARE)
    ]
    try:
        await _wait_until(lambda: len(running) == PROBE_TENANT_SHARE)
        waiting = asyncio.create_task(
            run_blocking_probe(_silent_peer, "cancelled", tenant_id=tenant, timeout=10)
        )
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        release.set()
    await asyncio.gather(*held)
    assert await run_blocking_probe(_silent_peer, "next", tenant_id=tenant, timeout=1) == "next"
    assert "cancelled" not in running


async def test_tenant_gets_its_share_back_when_its_probes_end() -> None:
    tenant = uuid.uuid4()
    for _ in range(PROBE_TENANT_SHARE + 1):
        assert await run_blocking_probe(lambda: "ok", tenant_id=tenant, timeout=1) == "ok"


async def test_share_stays_in_use_until_a_cancelled_probe_ends() -> None:
    release = threading.Event()
    running: list[int] = []

    def _silent_peer() -> str:
        running.append(1)
        release.wait(10)
        return "done"

    tenant = uuid.uuid4()
    held = [
        asyncio.create_task(run_blocking_probe(_silent_peer, tenant_id=tenant, timeout=10))
        for _ in range(PROBE_TENANT_SHARE)
    ]
    try:
        await _wait_until(lambda: len(running) == PROBE_TENANT_SHARE)
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)
        # The threads still wait on their sockets, so the share is still in use.
        with pytest.raises(ProbePoolBusyError):
            await run_blocking_probe(_silent_peer, tenant_id=tenant, timeout=0.2)
    finally:
        release.set()
    for _ in range(200):
        try:
            assert await run_blocking_probe(lambda: "ok", tenant_id=tenant, timeout=1) == "ok"
            break
        except ProbePoolBusyError:
            await asyncio.sleep(0.02)
    else:
        raise AssertionError("the share was not freed")


async def test_probe_that_waited_longer_than_its_timeout_does_not_start() -> None:
    release = threading.Event()
    started: list[str] = []

    def _silent_peer(name: str = "held") -> str:
        started.append(name)
        release.wait(10)
        return name

    probes = _fill_pool(_silent_peer)
    late = asyncio.create_task(
        run_blocking_probe(_silent_peer, "late", tenant_id=uuid.uuid4(), timeout=0.1)
    )
    try:
        await _wait_until(lambda: len(started) == PROBE_POOL_WORKERS)
        await asyncio.sleep(0.3)
    finally:
        release.set()
    with pytest.raises(ProbePoolBusyError, match="did not start"):
        await late
    await asyncio.gather(*probes)
    assert "late" not in started


async def test_probe_pool_returns_the_error_of_the_probe() -> None:
    def _fail() -> None:
        raise OSError("unreachable")

    with pytest.raises(OSError, match="unreachable"):
        await run_blocking_probe(_fail, tenant_id=uuid.uuid4(), timeout=1)


def test_probe_pool_busy_is_its_own_dispatch_error() -> None:
    exc = ProbePoolBusyError("this tenant already has 2 network probes running or waiting")
    assert is_probe_pool_busy(exc)
    assert not is_probe_pool_busy(RuntimeError("other"))
    result = result_probe_pool_busy("net.ping", exc, 1.0)
    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("probe_pool_busy:")
    assert result.extras is not None
    assert result.extras["error_code"] == "probe_pool_busy"
    assert result.extras["tenant_share"] == PROBE_TENANT_SHARE
    assert result.extras["exception_class"] == "ProbePoolBusyError"


# --- tls deadline ----------------------------------------------------------


class _FakeSocket:
    """Stand-in for ``socket.socket`` that records how it was made and used."""

    made: ClassVar[list[_FakeSocket]] = []
    connect_hook: ClassVar[Any] = None

    def __init__(self, family: int, socktype: int, proto: int) -> None:
        self.family, self.socktype, self.proto = family, socktype, proto
        self.timeout: float | None = None
        self.address: Any = None
        self.closed = False
        _FakeSocket.made.append(self)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, address: Any) -> None:
        self.address = address
        if _FakeSocket.connect_hook is not None:
            _FakeSocket.connect_hook(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSocket]:
    _FakeSocket.made = []
    _FakeSocket.connect_hook = None
    monkeypatch.setattr(net_tls.socket, "socket", _FakeSocket)
    return _FakeSocket


def test_tls_connect_shares_one_deadline_across_all_addresses(
    monkeypatch: pytest.MonkeyPatch, fake_socket: type[_FakeSocket]
) -> None:
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{n}", 443)) for n in range(1, 9)
    ]
    monkeypatch.setattr(net_tls.socket, "getaddrinfo", lambda *a, **k: addresses)

    def _slow_connect(sock: _FakeSocket) -> None:
        assert sock.timeout is not None
        time.sleep(sock.timeout)
        raise TimeoutError("timed out")

    fake_socket.connect_hook = _slow_connect
    begin = time.monotonic()
    with pytest.raises(TimeoutError):
        net_tls._connect_before_deadline("host.example", 443, time.monotonic() + 0.3)
    assert time.monotonic() - begin < 0.6
    granted = [sock.timeout for sock in fake_socket.made if sock.timeout is not None]
    assert len(granted) < len(addresses)
    assert max(granted) <= 0.3
    assert all(sock.closed for sock in fake_socket.made)


def test_tls_connect_tries_the_next_address_after_a_refusal(
    monkeypatch: pytest.MonkeyPatch, fake_socket: type[_FakeSocket]
) -> None:
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 443)),
    ]
    monkeypatch.setattr(net_tls.socket, "getaddrinfo", lambda *a, **k: addresses)

    def _connect(sock: _FakeSocket) -> None:
        if sock.address[0] == "192.0.2.1":
            raise ConnectionRefusedError

    fake_socket.connect_hook = _connect
    result = net_tls._connect_before_deadline("host.example", 443, time.monotonic() + 5)
    first, second = fake_socket.made
    assert result is second
    assert first.closed
    assert not second.closed


def test_tls_connect_tries_the_next_address_when_a_family_is_not_supported(
    monkeypatch: pytest.MonkeyPatch, fake_socket: type[_FakeSocket]
) -> None:
    addresses = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
    ]
    monkeypatch.setattr(net_tls.socket, "getaddrinfo", lambda *a, **k: addresses)

    def _socket(family: int, *args: Any) -> _FakeSocket:
        if family == socket.AF_INET6:
            raise OSError("address family not supported")
        return _FakeSocket(family, *args)

    monkeypatch.setattr(net_tls.socket, "socket", _socket)
    result = net_tls._connect_before_deadline("host.example", 443, time.monotonic() + 5)
    assert isinstance(result, _FakeSocket)
    assert result.address == ("192.0.2.1", 443)
    assert fake_socket.made == [result]


def test_tls_connect_keeps_the_scope_of_a_link_local_ipv6_address(
    monkeypatch: pytest.MonkeyPatch, fake_socket: type[_FakeSocket]
) -> None:
    sockaddr = ("fe80::1", 443, 0, 3)
    addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", sockaddr)]
    monkeypatch.setattr(net_tls.socket, "getaddrinfo", lambda *a, **k: addresses)
    sock = net_tls._connect_before_deadline("fe80::1%3", 443, time.monotonic() + 5)
    assert isinstance(sock, _FakeSocket)
    assert sock.family == socket.AF_INET6
    assert sock.address == sockaddr


# --- YAML aliases ----------------------------------------------------------


def test_loader_refuses_nested_aliases() -> None:
    with pytest.raises(yaml.YAMLError, match="aliases"):
        load_yaml_no_alias(_ALIAS_BOMB)


def test_loader_refuses_an_anchor_without_alias() -> None:
    with pytest.raises(yaml.YAMLError):
        load_yaml_no_alias("a: &x 1\n")


def test_loader_reads_plain_yaml_like_safe_load() -> None:
    text = "targets:\n  - name: t1\n    port: 443\n    tags: [a, b]\n"
    assert load_yaml_no_alias(text) == yaml.safe_load(text)


def test_targets_import_gives_the_parse_error_for_an_alias_file() -> None:
    text = "targets:\n  - name: t1\n    product: p\n    host: h\n    x: &e {k: v}\n    y: *e\n"
    with pytest.raises(ImportParseError, match="parse YAML"):
        _parse_targets_yaml(text)


def test_topology_import_gives_the_parse_error_for_an_alias_file() -> None:
    with pytest.raises(BulkParseError, match="could not parse"):
        parse_bulk_rows("edges: &e []\nmore: *e\n")


# --- extras limits ---------------------------------------------------------


def _body(extras: Any) -> dict[str, Any]:
    return {"name": "t1", "product": "p", "host": "93.184.216.34", "extras": extras}


def _nested(levels: int) -> dict[str, Any]:
    value: Any = "leaf"
    for _ in range(levels):
        value = {"k": value}
    return value


def test_extras_with_too_many_values_is_refused() -> None:
    too_many = {f"k{n}": n for n in range(EXTRAS_MAX_NODES)}
    with pytest.raises(ValidationError, match="at most"):
        TargetCreate(**_body(too_many))
    with pytest.raises(ValidationError, match="at most"):
        TargetUpdate(extras=too_many)


def test_extras_that_repeat_one_object_count_every_copy() -> None:
    shared = ["x"] * 100
    repeated = {f"k{n}": shared for n in range(40)}
    with pytest.raises(ValidationError, match="at most"):
        TargetCreate(**_body(repeated))


def test_a_very_wide_list_is_refused_without_reading_its_items() -> None:
    wide = [0] * 200_000
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="at most"):
            check_extras_size({"items": wide})
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100_000


def test_extras_at_the_limit_are_accepted() -> None:
    at_limit = [0] * (EXTRAS_MAX_NODES - 1)
    assert check_extras_size(at_limit) is at_limit
    with pytest.raises(ValueError, match="at most"):
        check_extras_size([*at_limit, 0])


def test_extras_nested_too_deep_is_refused() -> None:
    with pytest.raises(ValidationError, match="levels deep"):
        TargetCreate(**_body(_nested(EXTRAS_MAX_DEPTH + 2)))
    with pytest.raises(ValidationError, match="levels deep"):
        TargetUpdate(extras=_nested(EXTRAS_MAX_DEPTH + 2))


def test_normal_extras_are_accepted() -> None:
    extras = {"site": "a", "ports": [1, 2, 3], "nested": _nested(3)}
    assert TargetCreate(**_body(extras)).extras == extras
    assert TargetCreate(**{**_body({}), "extras": {}}).extras == {}
    assert TargetUpdate(extras=None).extras is None
    assert TargetUpdate(extras=extras).extras == extras


# --- login flows -----------------------------------------------------------


async def test_pending_login_flows_never_exceed_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meho_backplane.ui.auth.verifier_store.MAX_PENDING_FLOWS", 5)
    store = PKCEVerifierStore()
    for n in range(12):
        await store.put(f"s{n}", code_verifier="v", return_to="/ui/", browser_binding="b")
        assert store.size() <= 5
    assert store.size() == 5
    oldest = await store.pop("s0")
    newest = await store.pop("s11")
    assert oldest is None
    assert newest is not None


async def test_repeated_state_does_not_grow_the_store() -> None:
    store = PKCEVerifierStore()
    for _ in range(3):
        await store.put("same", code_verifier="v", return_to="/ui/", browser_binding="b")
    assert store.size() == 1


def test_cap_constant_is_ten_thousand() -> None:
    assert MAX_PENDING_FLOWS == 10_000


def test_long_return_to_becomes_the_default_path() -> None:
    long_path = "/ui/" + "a" * 2100
    assert auth_routes._safe_return_to(long_path) == "/ui/"
    ok_path = "/ui/" + "a" * 100
    assert auth_routes._safe_return_to(ok_path) == ok_path
