# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the connector package set digest on the runner side (#5038).

The runner sends the digest of its connector packages on every call and
refuses a work item that needs another package set. The central side
(leaving such items out of the assignment) is tested in
``test_gateway_assignment_api.py``. The package names here are made up.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from meho_backplane import connector_packages
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connector_packages import ConnectorPackage
from meho_backplane.runner import executor
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.executor import execute_work_item
from meho_backplane.runner.package_set import (
    EMPTY_PACKAGE_SET_DIGEST,
    PACKAGE_SET_HEADER,
    PackageSet,
    local_package_set,
    package_set_digest,
    package_set_mismatch_message,
)
from meho_backplane.runner.wire import RunnerPrincipal, RunnerWorkItem

_ALLOWLIST_ENV = "MEHO_NETDIAG_PROBE_ALLOWLIST"
_OTHER_PACKAGE_SET = "sha256:" + "ab" * 32


def _package(
    name: str = "acme-widget", version: str = "1.0.0", **overrides: Any
) -> ConnectorPackage:
    fields: dict[str, Any] = {
        "name": name,
        "version": version,
        "wheel_digest": "sha256:" + "1" * 64,
        "folders": ["acme_widget"],
    }
    fields.update(overrides)
    return ConnectorPackage(**fields)


@pytest.fixture
def allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the allowlist file at a temp path and reset the cached package set."""
    path = tmp_path / "connector-packages.json"
    monkeypatch.setattr(connector_packages, "CONNECTOR_PACKAGES_FILE", path)
    local_package_set.cache_clear()
    yield path
    local_package_set.cache_clear()


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def test_digest_shape_and_empty_set() -> None:
    assert package_set_digest([]) == EMPTY_PACKAGE_SET_DIGEST
    assert EMPTY_PACKAGE_SET_DIGEST.startswith("sha256:")
    assert len(EMPTY_PACKAGE_SET_DIGEST) == len("sha256:") + 64


def test_digest_ignores_order_and_folders() -> None:
    a = _package("acme-a", folders=["acme_a"])
    b = _package("acme-b", folders=["acme_b"])

    assert package_set_digest([a, b]) == package_set_digest([b, a])
    assert package_set_digest([a]) == package_set_digest([_package("acme-a", folders=["other"])])


@pytest.mark.parametrize(
    "changed",
    [
        _package("acme-other"),
        _package(version="1.0.1"),
        _package(wheel_digest="sha256:" + "2" * 64),
    ],
)
def test_digest_changes_with_name_version_or_wheel(changed: ConnectorPackage) -> None:
    assert package_set_digest([changed]) != package_set_digest([_package()])
    assert package_set_digest([changed]) != EMPTY_PACKAGE_SET_DIGEST


def test_package_for_handler_maps_connector_folders() -> None:
    packages = PackageSet.of([_package()])

    assert packages.package_for_handler("meho_backplane.connectors.acme_widget.ops.read") == (
        "acme-widget"
    )
    assert packages.package_for_handler("meho_backplane.connectors.net.ops.net_tcp_check") is None
    assert packages.package_for_handler("other.acme_widget.ops.read") is None


def test_local_package_set_without_allowlist_is_empty(allowlist: Path) -> None:
    assert not allowlist.exists()
    assert local_package_set().digest == EMPTY_PACKAGE_SET_DIGEST


def test_local_package_set_reads_the_allowlist(allowlist: Path) -> None:
    allowlist.write_text(json.dumps({"packages": [_package().model_dump()]}))

    assert local_package_set().digest == package_set_digest([_package()])
    assert dict(local_package_set().folders) == {"acme_widget": "acme-widget"}


# ---------------------------------------------------------------------------
# The runner client sends it
# ---------------------------------------------------------------------------


async def test_client_sends_the_package_set_on_every_call(allowlist: Path) -> None:
    allowlist.write_text(json.dumps({"packages": [_package().model_dump()]}))
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(304)

    async with RunnerClient(
        central_url="https://central.test",
        runner_id="runner-7",
        token="tok",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.fetch_assignment(known_version="digest-abc")

    assert captured[0].headers[PACKAGE_SET_HEADER] == package_set_digest([_package()])


# ---------------------------------------------------------------------------
# The runner checks it before it runs an item
# ---------------------------------------------------------------------------


class _LogRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})

    def info(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})


def _tcp_check_item(*, port: int, package_set: str | None) -> RunnerWorkItem:
    return RunnerWorkItem(
        check_ref="chk-pkg",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref="meho_backplane.connectors.net.ops.net_tcp_check",
        params={"host": "127.0.0.1", "port": port},
        safety_level="safe",
        principal=RunnerPrincipal(
            sub="runner-svc", tenant_id=uuid.uuid4(), tenant_role=TenantRole.READ_ONLY
        ),
        package_set=package_set,
    )


async def test_item_for_the_same_package_set_runs(
    allowlist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await execute_work_item(
            _tcp_check_item(port=port, package_set=local_package_set().digest)
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "ok"
    assert result.result is not None
    assert result.result["connected"] is True


async def test_item_for_another_package_set_is_refused_with_both_digests(
    allowlist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner that gets the item anyway refuses it before any import, naming both digests."""
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    log = _LogRecorder()
    monkeypatch.setattr(executor, "_log", log)

    def _must_not_import(ref: str) -> Any:
        raise AssertionError(f"handler imported: {ref}")

    monkeypatch.setattr(executor, "import_handler", _must_not_import)

    result = await execute_work_item(_tcp_check_item(port=9, package_set=_OTHER_PACKAGE_SET))

    expected = package_set_mismatch_message(EMPTY_PACKAGE_SET_DIGEST, _OTHER_PACKAGE_SET)
    assert result.status == "refused"
    assert result.error == expected
    assert EMPTY_PACKAGE_SET_DIGEST in expected
    assert _OTHER_PACKAGE_SET in expected
    assert log.events == [
        {
            "event": "runner_item_refused",
            "op_id": "net.tcp_check",
            "check_ref": "chk-pkg",
            "reason": expected,
        }
    ]
