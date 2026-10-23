# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the vCenter copy helper (``vmware_rest/_transfer.py``, #4343).

Every HTTP call is mocked with ``respx``. The source is a streaming vCenter
download; the destination accepts ``HEAD`` / ``PUT`` / ``MOVE`` / ``DELETE``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import sys
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx
import structlog
from structlog.testing import capture_logs

from meho_backplane.connectors.vmware_rest import _transfer
from meho_backplane.connectors.vmware_rest._transfer import (
    PARTIAL_SUFFIX,
    Destination,
    SourceFile,
    TransferError,
    copy_files,
    is_meho_temp_name,
    meho_temp_name,
)
from meho_backplane.logging import _STDLIB_BRIDGE_HANDLER_NAME, configure_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SRC = "https://vcenter.example.test/cls-data/session-1"
DEST = "https://store.example.test/backups/"
TOKEN = "dest-token-value"
SESSION = "vcenter-session-value"


class _Target:
    name = "vc-1"
    host = "vcenter.example.test"


class _Calls:
    def __init__(self) -> None:
        self.keep_alive = 0
        self.cleanup = 0

    async def on_keep_alive(self) -> None:
        self.keep_alive += 1

    async def on_cleanup(self) -> None:
        self.cleanup += 1


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load(target: Any, operator: Any, *, param: str, mount: str) -> dict[str, Any]:
        assert target.secret_ref == "tenants/t1/backup-store"
        assert param == "destination_token_ref"
        return {"token": TOKEN}

    monkeypatch.setattr(_transfer, "load_param_secret_data", fake_load)


def _stream(*chunks: bytes, fail: bool = False, delay: float = 0.0) -> AsyncIterator[bytes]:
    async def gen() -> AsyncIterator[bytes]:
        for chunk in chunks:
            if delay:
                await asyncio.sleep(delay)
            yield chunk
        if fail:
            raise httpx.ReadError("connection reset")

    return gen()


def _source(mock: respx.MockRouter, name: str, *chunks: bytes, **kw: Any) -> respx.Route:
    length = {"content-length": str(sum(len(c) for c in chunks))}
    return mock.get(f"{SRC}/{name}").mock(
        return_value=httpx.Response(200, headers=length, content=_stream(*chunks, **kw))
    )


def _destination(
    mock: respx.MockRouter,
    name: str,
    *,
    final_head: int = 404,
    partial_head: int = 404,
    put: int = 201,
    move: int = 201,
) -> dict[str, respx.Route]:
    final = f"{DEST}{name}"
    partial = f"{final}{PARTIAL_SUFFIX}"
    return {
        "head_final": mock.head(final).mock(return_value=httpx.Response(final_head)),
        "head_partial": mock.head(partial).mock(return_value=httpx.Response(partial_head)),
        "delete_partial": mock.delete(partial).mock(return_value=httpx.Response(204)),
        "put": mock.put(partial).mock(return_value=httpx.Response(put)),
        "move": mock.route(method="MOVE", url=partial).mock(return_value=httpx.Response(move)),
    }


async def _run(files: list[SourceFile], calls: _Calls, **kw: Any) -> _transfer.TransferResult:
    async with httpx.AsyncClient() as source_client:
        return await copy_files(
            files,
            Destination(base_url=DEST, token_ref="tenants/t1/backup-store"),
            operator=object(),  # type: ignore[arg-type]
            target=_Target(),
            source_client=source_client,
            source_headers={"vmware-api-session-id": SESSION},
            keep_alive=calls.on_keep_alive,
            cleanup=calls.on_cleanup,
            **kw,
        )


def _file(name: str, size: int | None = None) -> SourceFile:
    return SourceFile(name=name, url=f"{SRC}/{name}?token=vc-download-secret", size=size)


@respx.mock(assert_all_called=False)
async def test_good_copy_of_two_files(respx_mock: respx.MockRouter) -> None:
    a = [b"a" * 1000, b"b" * 24]
    b = [b"c" * 10]
    src1 = _source(respx_mock, "disk-1.vmdk", *a)
    _source(respx_mock, "vm.ovf", *b)
    d1 = _destination(respx_mock, "disk-1.vmdk")
    d2 = _destination(respx_mock, "vm.ovf")
    calls = _Calls()

    with capture_logs() as logs:
        result = await _run([_file("disk-1.vmdk", 1024), _file("vm.ovf")], calls)

    rows = result.as_dict()["files"]
    assert rows == [
        {
            "name": "disk-1.vmdk",
            "size": 1024,
            "sha256": hashlib.sha256(b"".join(a)).hexdigest(),
            "destination_path": "/backups/disk-1.vmdk",
            "status": "done",
        },
        {
            "name": "vm.ovf",
            "size": 10,
            "sha256": hashlib.sha256(b"".join(b)).hexdigest(),
            "destination_path": "/backups/vm.ovf",
            "status": "done",
        },
    ]
    assert calls.cleanup == 1
    put = d1["put"].calls.last.request
    assert put.headers["authorization"] == f"Bearer {TOKEN}"
    assert put.headers["content-length"] == "1024"
    assert put.content == b"".join(a)
    move = d1["move"].calls.last.request
    assert move.headers["destination"] == f"{DEST}disk-1.vmdk"
    assert move.headers["overwrite"] == "F"
    assert d2["move"].called
    source_request = src1.calls.last.request
    assert source_request.headers["vmware-api-session-id"] == SESSION
    assert "authorization" not in source_request.headers
    text = json.dumps(result.as_dict()) + repr(logs)
    for secret in (TOKEN, SESSION, "vc-download-secret"):
        assert secret not in text


@respx.mock(assert_all_called=False)
async def test_source_download_asks_for_raw_bytes(respx_mock: respx.MockRouter) -> None:
    # A gzip answer would make Content-Length the packed size, not the copied size.
    source = _source(respx_mock, "vm.ovf", b"c" * 10)
    _destination(respx_mock, "vm.ovf")

    async with httpx.AsyncClient() as source_client:
        await copy_files(
            [_file("vm.ovf")],
            Destination(base_url=DEST, token_ref="tenants/t1/backup-store"),
            operator=object(),  # type: ignore[arg-type]
            target=_Target(),
            source_client=source_client,
            source_headers={"vmware-api-session-id": SESSION, "accept-encoding": "gzip"},
            keep_alive=_Calls().on_keep_alive,
            cleanup=_Calls().on_cleanup,
        )

    request = source.calls.last.request
    assert request.headers.get_list("accept-encoding") == ["identity"]
    assert request.headers["vmware-api-session-id"] == SESSION


def _undo_production_logging() -> None:
    structlog.reset_defaults()
    root = logging.getLogger()
    for handler in [h for h in root.handlers if h.name == _STDLIB_BRIDGE_HANDLER_NAME]:
        root.removeHandler(handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = True


@respx.mock(assert_all_called=False)
async def test_production_logging_hides_the_download_url(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx logs every request at INFO through Python's ``logging``.

    With the production logging, that line reaches stdout. It must not carry
    the vCenter download URL: its path and query are the credential.
    """
    _source(respx_mock, "vm.ovf", b"c" * 10)
    _destination(respx_mock, "vm.ovf")
    # Another library's dictConfig can switch the httpx logger off in the test session.
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    # A fresh structlog proxy, so the JSON logger this test caches does not outlive it.
    monkeypatch.setattr(_transfer, "_log", structlog.get_logger(_transfer.__name__))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    configure_logging(level=logging.INFO)
    try:
        with caplog.at_level(logging.INFO):
            await _run([_file("vm.ovf")], _Calls())
    finally:
        _undo_production_logging()

    text = stdout.getvalue() + caplog.text
    assert "HTTP Request: GET https://vcenter.example.test " in text
    assert "HTTP Request: PUT https://store.example.test " in text
    assert "vcenter_transfer_file_copied" in stdout.getvalue()
    for secret in ("vc-download-secret", "cls-data", "session-1", TOKEN, SESSION):
        assert secret not in text, secret


@respx.mock(assert_all_called=False)
async def test_source_breaks_in_the_middle(respx_mock: respx.MockRouter) -> None:
    _source(respx_mock, "disk-1.vmdk", b"x" * 100, fail=True)
    routes = _destination(respx_mock, "disk-1.vmdk")
    calls = _Calls()

    with pytest.raises(TransferError, match="connection failed") as info:
        await _run([_file("disk-1.vmdk")], calls)

    assert calls.cleanup == 1
    assert not routes["move"].called
    [row] = info.value.files
    assert row.status == "failed"
    assert row.destination_path == "/backups/disk-1.vmdk.meho-partial"
    assert "vc-download-secret" not in str(info.value)


@respx.mock(assert_all_called=False)
async def test_destination_answers_507(respx_mock: respx.MockRouter) -> None:
    _source(respx_mock, "disk-1.vmdk", b"x" * 10)
    routes = _destination(respx_mock, "disk-1.vmdk", put=507)
    calls = _Calls()

    with pytest.raises(TransferError, match="HTTP 507 Insufficient Storage"):
        await _run([_file("disk-1.vmdk")], calls)

    assert calls.cleanup == 1
    assert not routes["move"].called


@respx.mock(assert_all_called=False)
async def test_file_already_at_destination(respx_mock: respx.MockRouter) -> None:
    source = _source(respx_mock, "disk-1.vmdk", b"x" * 10)
    _destination(respx_mock, "disk-1.vmdk", final_head=200)
    calls = _Calls()

    with pytest.raises(TransferError, match="already at the destination"):
        await _run([_file("disk-1.vmdk")], calls)

    assert not source.called
    assert calls.cleanup == 1


@respx.mock(assert_all_called=False)
async def test_overwrite_replaces_a_file_at_destination(respx_mock: respx.MockRouter) -> None:
    _source(respx_mock, "disk-1.vmdk", b"x" * 10)
    routes = _destination(respx_mock, "disk-1.vmdk", final_head=200)

    result = await _run([_file("disk-1.vmdk")], _Calls(), overwrite=True)

    assert result.files[0].status == "done"
    assert routes["move"].calls.last.request.headers["overwrite"] == "T"


@respx.mock(assert_all_called=False)
async def test_no_write_access_at_destination(respx_mock: respx.MockRouter) -> None:
    _destination(respx_mock, "disk-1.vmdk", final_head=403)

    with pytest.raises(TransferError, match="may not write"):
        await _run([_file("disk-1.vmdk")], _Calls())


@respx.mock(assert_all_called=False)
async def test_size_mismatch(respx_mock: respx.MockRouter) -> None:
    _source(respx_mock, "disk-1.vmdk", b"x" * 50)
    routes = _destination(respx_mock, "disk-1.vmdk")
    calls = _Calls()

    with pytest.raises(TransferError, match="sent 50 bytes, expected 100") as info:
        await _run([_file("disk-1.vmdk", 100)], calls)

    assert not routes["move"].called
    assert info.value.files[0].status == "failed"
    assert calls.cleanup == 1


@respx.mock(assert_all_called=False)
async def test_keep_alive_runs_during_a_long_copy(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_transfer, "_KEEP_ALIVE_INTERVAL", 0.01)
    _source(respx_mock, "disk-1.vmdk", b"x" * 10, b"y" * 10, b"z" * 10, delay=0.05)
    _destination(respx_mock, "disk-1.vmdk")
    calls = _Calls()

    await _run([_file("disk-1.vmdk")], calls)

    assert calls.keep_alive >= 1


@respx.mock(assert_all_called=False)
async def test_redirect_to_another_host_is_refused(respx_mock: respx.MockRouter) -> None:
    _source(respx_mock, "disk-1.vmdk", b"x" * 10)
    routes = _destination(respx_mock, "disk-1.vmdk")
    routes["put"].mock(
        return_value=httpx.Response(307, headers={"location": "https://other.example.test/x"})
    )
    other = respx_mock.route(host="other.example.test").mock(return_value=httpx.Response(201))
    calls = _Calls()

    with pytest.raises(TransferError, match="another host"):
        await _run([_file("disk-1.vmdk")], calls)

    assert not other.called
    assert not routes["move"].called
    assert calls.cleanup == 1


@respx.mock(assert_all_called=False)
async def test_source_redirect_to_another_host_is_refused(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{SRC}/disk-1.vmdk").mock(
        return_value=httpx.Response(302, headers={"location": "https://other.example.test/x"})
    )
    _destination(respx_mock, "disk-1.vmdk")
    other = respx_mock.route(host="other.example.test").mock(return_value=httpx.Response(200))

    with pytest.raises(TransferError, match="another host"):
        await _run([_file("disk-1.vmdk")], _Calls())

    assert not other.called


@respx.mock(assert_all_called=False)
async def test_leftovers_of_an_earlier_run_are_removed(respx_mock: respx.MockRouter) -> None:
    _source(respx_mock, "disk-1.vmdk", b"x" * 10)
    routes = _destination(respx_mock, "disk-1.vmdk", partial_head=200)
    stale = meho_temp_name("export")

    async def remove_leftovers() -> list[str]:
        return [stale]

    result = await _run([_file("disk-1.vmdk")], _Calls(), remove_leftovers=remove_leftovers)

    assert routes["delete_partial"].called
    assert routes["delete_partial"].calls.last.request.headers["authorization"] == (
        f"Bearer {TOKEN}"
    )
    assert result.leftovers_removed == [stale, "/backups/disk-1.vmdk.meho-partial"]
    assert is_meho_temp_name(stale)
    assert not is_meho_temp_name("export-1")
    assert result.files[0].status == "done"


@respx.mock(assert_all_called=False)
async def test_destination_without_rename_marks_the_row_partial(
    respx_mock: respx.MockRouter,
) -> None:
    _source(respx_mock, "disk-1.vmdk", b"x" * 10)
    _destination(respx_mock, "disk-1.vmdk", move=405)

    result = await _run([_file("disk-1.vmdk")], _Calls())

    assert result.files[0].status == "partial"
    assert result.files[0].destination_path == "/backups/disk-1.vmdk.meho-partial"


@respx.mock(assert_all_called=False)
async def test_file_size_limit_stops_the_copy(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{SRC}/disk-1.vmdk").mock(
        return_value=httpx.Response(200, content=_stream(b"x" * 10, b"y" * 10))
    )
    routes = _destination(respx_mock, "disk-1.vmdk")

    with pytest.raises(TransferError, match="passed the size limit"):
        await _run([_file("disk-1.vmdk")], _Calls(), max_file_bytes=15)

    assert not routes["move"].called


async def test_total_size_limit_is_checked_before_any_copy() -> None:
    calls = _Calls()
    files = [_file("a.bin", 10), _file("b.bin", 10)]

    with pytest.raises(TransferError, match="total size limit"):
        await _run(files, calls, max_total_bytes=15)

    assert calls.cleanup == 1


@pytest.mark.parametrize(
    "base_url",
    ["http://store.example.test/b/", "https://user:pw@store.example.test/b/", f"{DEST}?sig=x"],
)
async def test_unsafe_destination_base_url_is_refused(base_url: str) -> None:
    async with httpx.AsyncClient() as source_client:
        with pytest.raises(TransferError, match="destination base URL"):
            await copy_files(
                [_file("a.bin")],
                Destination(base_url=base_url, token_ref="tenants/t1/backup-store"),
                operator=object(),  # type: ignore[arg-type]
                target=_Target(),
                source_client=source_client,
                source_headers={},
                keep_alive=_Calls().on_keep_alive,
                cleanup=_Calls().on_cleanup,
            )


@pytest.mark.parametrize("name", ["../etc/passwd", "a//b", "", "a/./b"])
async def test_unsafe_file_name_is_refused(name: str) -> None:
    calls = _Calls()
    with pytest.raises(TransferError, match="not a safe destination path"):
        await _run([_file(name)], calls)
    assert calls.cleanup == 1


@pytest.mark.parametrize(
    ("names", "reason"),
    [
        (["disk.meho-partial", "disk"], "keeps for its temporary files"),
        (["disk", "disk.meho-partial/x"], "keeps for its temporary files"),
        (["disk", "disk"], "listed twice"),
    ],
)
@respx.mock(assert_all_called=False)
async def test_reserved_or_repeated_names_are_refused_before_any_change(
    respx_mock: respx.MockRouter, names: list[str], reason: str
) -> None:
    # "disk.meho-partial" is the temporary name of "disk". Without the check,
    # copying "disk" after it would delete the first file as a leftover.
    for name in names:
        _source(respx_mock, name, b"x" * 10)
        _destination(respx_mock, name)
    calls = _Calls()

    with pytest.raises(TransferError, match=reason) as info:
        await _run([_file(name) for name in names], calls)

    assert not respx_mock.calls
    assert info.value.files == []
    assert calls.cleanup == 1


async def test_destination_on_a_blocked_address_is_refused() -> None:
    calls = _Calls()
    async with httpx.AsyncClient() as source_client:
        with pytest.raises(TransferError, match="not a public address") as info:
            await copy_files(
                [_file("disk-1.vmdk")],
                Destination(
                    base_url="https://169.254.169.254/b/", token_ref="tenants/t1/backup-store"
                ),
                operator=object(),  # type: ignore[arg-type]
                target=_Target(),
                source_client=source_client,
                source_headers={},
                keep_alive=calls.on_keep_alive,
                cleanup=calls.on_cleanup,
            )
    assert "connection failed" not in str(info.value)
    assert calls.cleanup == 1
