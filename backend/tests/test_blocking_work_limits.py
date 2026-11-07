# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Limits on work that one caller can start: probes, imports and login flows."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from meho_backplane.connectors.net import tls as net_tls
from meho_backplane.connectors.net.probe_pool import PROBE_POOL_WORKERS, run_blocking_probe
from meho_backplane.targets.extras_limits import EXTRAS_MAX_DEPTH, EXTRAS_MAX_NODES
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.ui.auth import routes as auth_routes
from meho_backplane.ui.auth.verifier_store import MAX_PENDING_FLOWS, PKCEVerifierStore
from meho_backplane.ui.routes.connectors.import_view import ImportParseError, _parse_targets_yaml
from meho_backplane.ui.routes.topology.batch_parse import BulkParseError, parse_bulk_rows
from meho_backplane.ui.safe_yaml import load_yaml_no_alias

_ALIAS_BOMB = """
a: &a [x, x, x, x, x, x, x, x, x, x]
b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a, *a]
c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b, *b]
d: [*c, *c, *c, *c, *c, *c, *c, *c, *c, *c]
"""


# --- probe pool ------------------------------------------------------------


async def test_waiting_probes_leave_the_default_thread_pool_free() -> None:
    release = threading.Event()
    running: list[int] = []

    def _silent_peer() -> str:
        running.append(1)
        release.wait(10)
        return "done"

    probes = [
        asyncio.create_task(run_blocking_probe(_silent_peer)) for _ in range(PROBE_POOL_WORKERS)
    ]
    # One more probe than the pool has threads: it waits in the probe queue.
    queued = asyncio.create_task(run_blocking_probe(_silent_peer))
    try:
        for _ in range(100):
            if len(running) == PROBE_POOL_WORKERS:
                break
            await asyncio.sleep(0.05)
        assert len(running) == PROBE_POOL_WORKERS
        begin = time.monotonic()
        assert await asyncio.to_thread(lambda: "login ok") == "login ok"
        assert time.monotonic() - begin < 0.1
        assert not queued.done()
    finally:
        release.set()
    assert await asyncio.gather(*probes, queued) == ["done"] * (PROBE_POOL_WORKERS + 1)


async def test_probe_pool_returns_the_error_of_the_probe() -> None:
    def _fail() -> None:
        raise OSError("unreachable")

    with pytest.raises(OSError, match="unreachable"):
        await run_blocking_probe(_fail)


# --- tls deadline ----------------------------------------------------------


def test_tls_connect_shares_one_deadline_across_all_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{n}", 443)) for n in range(1, 9)
    ]
    monkeypatch.setattr(net_tls.socket, "getaddrinfo", lambda *a, **k: addresses)
    granted: list[float] = []

    def _slow_connect(_addr: Any, timeout: float) -> None:
        granted.append(timeout)
        time.sleep(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr(net_tls.socket, "create_connection", _slow_connect)
    begin = time.monotonic()
    with pytest.raises(TimeoutError):
        net_tls._connect_before_deadline("host.example", 443, time.monotonic() + 0.3)
    assert time.monotonic() - begin < 0.6
    assert len(granted) < len(addresses)
    assert max(granted) <= 0.3


def test_tls_connect_tries_the_next_address_after_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.2", 443)),
    ]
    monkeypatch.setattr(net_tls.socket, "getaddrinfo", lambda *a, **k: addresses)
    sentinel = object()

    def _connect(addr: Any, timeout: float) -> object:
        if addr[0] == "192.0.2.1":
            raise ConnectionRefusedError
        return sentinel

    monkeypatch.setattr(net_tls.socket, "create_connection", _connect)
    assert net_tls._connect_before_deadline("host.example", 443, time.monotonic() + 5) is sentinel


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
    assert await store.pop("s0") is None
    assert await store.pop("s11") is not None


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
