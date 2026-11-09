# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests of the session store parts (#4901): plan, archive, check, S3 client.

Every secret and key in this file is made up.
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.response import StreamingBody
from botocore.stub import Stubber

from meho_backplane.session_store.archive import ArchiveError, read_archive, write_archive
from meho_backplane.session_store.config import SessionStoreConfig, load_config
from meho_backplane.session_store.object_store import ObjectStoreError, S3ObjectStore
from meho_backplane.session_store.plan import plan_upload, sha256_hex
from meho_backplane.session_store.secret_check import check_part

_LIMITS: dict[str, int] = {"max_files": 10, "max_unpacked_bytes": 1000}


def _state(data: bytes) -> dict[str, Any]:
    return {"size": len(data), "sha256": sha256_hex(data)}


# --- plan --------------------------------------------------------------------


def test_plan_new_files_are_whole_tails() -> None:
    plan = plan_upload({}, [], {"a": b"one\n", "b": b""})
    assert plan.tails == {"a": (0, b"one\n"), "b": (0, b"")}
    assert plan.conflicts == []


def test_plan_finds_the_tail_and_keeps_a_missing_file() -> None:
    stored = {"a": _state(b"one\n"), "gone": _state(b"x")}
    plan = plan_upload(stored, [stored], {"a": b"one\ntwo\n"})
    assert plan.tails == {"a": (4, b"two\n")}
    assert plan.states["gone"].size == 1
    assert plan.new_bytes == 4


def test_plan_same_or_older_state_is_no_change() -> None:
    first = {"a": _state(b"one\n")}
    second = {"a": _state(b"one\ntwo\n")}
    plan = plan_upload(second, [first, second], {"a": b"one\n"})
    assert plan.tails == {}
    assert plan.conflicts == []


@pytest.mark.parametrize(
    "new", [pytest.param(b"ONE\ntwo\n", id="changed-prefix"), pytest.param(b"on", id="shorter")]
)
def test_plan_changed_stored_part_is_a_conflict(new: bytes) -> None:
    stored = {"a": _state(b"one\n")}
    plan = plan_upload(stored, [stored], {"a": new})
    assert plan.conflicts == ["a"]
    assert plan.tails == {}


# --- archive -----------------------------------------------------------------


def _tar(members: list[tarfile.TarInfo], contents: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info in members:
            data = (contents or {}).get(info.name, b"")
            info.size = len(data) if info.isreg() else 0
            tar.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return buffer.getvalue()


def test_archive_round_trip_skips_folders() -> None:
    folder = tarfile.TarInfo("s")
    folder.type = tarfile.DIRTYPE
    data = _tar([folder, tarfile.TarInfo("./s/a.jsonl")], {"./s/a.jsonl": b"x"})
    assert read_archive(data, **_LIMITS) == {"s/a.jsonl": b"x"}
    assert read_archive(write_archive({"b": b"1", "a": b"2"}), **_LIMITS) == {"a": b"2", "b": b"1"}


def test_archive_refuses_a_link() -> None:
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(ArchiveError) as exc:
        read_archive(_tar([link]), **_LIMITS)
    assert exc.value.reason == "archive_bad_member"


@pytest.mark.parametrize("name", ["a/../b", "a\\b", "a//b", "bad\x07name"])
def test_archive_refuses_a_bad_path(name: str) -> None:
    with pytest.raises(ArchiveError) as exc:
        read_archive(_tar([tarfile.TarInfo(name)]), **_LIMITS)
    assert exc.value.reason == "archive_bad_path"


def test_archive_refuses_a_path_twice() -> None:
    data = _tar([tarfile.TarInfo("a"), tarfile.TarInfo("./a")])
    with pytest.raises(ArchiveError) as exc:
        read_archive(data, **_LIMITS)
    assert exc.value.reason == "archive_bad_path"


def test_archive_limits() -> None:
    many = _tar([tarfile.TarInfo(f"f{n}") for n in range(11)])
    with pytest.raises(ArchiveError) as exc:
        read_archive(many, **_LIMITS)
    assert exc.value.reason == "archive_too_large"
    big = _tar([tarfile.TarInfo("big")], {"big": b"x" * 1001})
    with pytest.raises(ArchiveError) as exc:
        read_archive(big, **_LIMITS)
    assert exc.value.reason == "archive_too_large"


def test_archive_cut_off_is_unreadable() -> None:
    data = write_archive({"a": b"x" * 5000})
    with pytest.raises(ArchiveError) as exc:
        read_archive(data[: len(data) // 2], **_LIMITS | {"max_unpacked_bytes": 10_000})
    assert exc.value.reason == "archive_unreadable"


# --- secret check --------------------------------------------------------------


def test_check_keeps_a_clean_line_byte_for_byte() -> None:
    line = b'{"usage": {"input_tokens": 12, "max_tokens": 4096}, "text": "ok"}\n'
    part = check_part(line)
    assert part.content == line
    assert not part.unclear
    assert part.redactions == {}


def test_check_replaces_a_known_secret_and_keeps_the_json_valid() -> None:
    line = b'{"cmd": "curl -H \\"Authorization: Bearer madeUpToken123456\\" x"}\n'
    part = check_part(line)
    assert b"madeUpToken123456" not in part.content
    assert json.loads(part.content)["cmd"] == 'curl -H "[REDACTED:authorization_header]" x'
    assert part.redactions == {"authorization_header": 1}
    assert not part.unclear


def test_check_replaces_a_known_secret_in_plain_text() -> None:
    part = check_part(b"export DB_PASSWORD=madeUpValue99\nnext line")
    assert part.content == b"export DB_[REDACTED:api_key]\nnext line"


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b'{"input": {"password": "Planted-Not-Real-1"}}', id="json-pair"),
        pytest.param(b'{"text": "{\\"secret\\": \\"Planted-Not-Real-1\\"}"}', id="escaped-json"),
        pytest.param(b'{"input": {"API_TOKEN": "Planted-Not-Real-1"}}', id="name-suffix"),
    ],
)
def test_check_is_not_sure_about_a_secret_like_pair(line: bytes) -> None:
    part = check_part(line)
    assert part.unclear_reasons == ("possible_secret",)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b'{"input": {"password": "${DB_PASSWORD}"}}', id="variable"),
        pytest.param(b'{"input": {"token": "abc"}}', id="short"),
        pytest.param(b'{"input": {"password": "[REDACTED:api_key]"}}', id="marker"),
    ],
)
def test_check_is_sure_about_placeholders(line: bytes) -> None:
    assert not check_part(line).unclear


def test_check_is_not_sure_about_bytes_that_are_not_text() -> None:
    part = check_part(b"\xff\xfe\x00binary")
    assert part.unclear_reasons == ("not_utf8_text",)
    assert part.content == b"\xff\xfe\x00binary"


# --- config ----------------------------------------------------------------------


def test_config_defaults_and_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SESSION_STORE_ENDPOINT", "SESSION_STORE_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SESSION_STORE_CREDENTIALS_VAULT_PATH", "secret/data/x")
    config = load_config()
    assert not config.configured
    assert config.retention_days_default == 90
    assert config.region == "us-east-1"
    monkeypatch.setenv("SESSION_STORE_ENDPOINT", "https://objects.example.com")
    monkeypatch.setenv("SESSION_STORE_BUCKET", "b")
    assert load_config().configured
    monkeypatch.setenv("SESSION_STORE_RETENTION_DAYS_DEFAULT", "0")
    with pytest.raises(ValueError, match="SESSION_STORE_RETENTION_DAYS_DEFAULT"):
        load_config()


# --- S3 client -------------------------------------------------------------------

_CONFIG = SessionStoreConfig(
    endpoint="https://objects.example.com",
    bucket="sessions",
    credentials_vault_path="secret/data/meho/session-store",
    region="us-east-1",
    retention_days_default=90,
    max_upload_bytes=100,
    max_unpacked_bytes=100,
    max_files=10,
    cleanup_enabled=True,
    cleanup_interval_seconds=3600,
)


def _stubbed() -> tuple[S3ObjectStore, Stubber, list[tuple[str, str]]]:
    client = boto3.client(
        "s3",
        endpoint_url=_CONFIG.endpoint,
        region_name="us-east-1",
        aws_access_key_id="made-up-id",
        aws_secret_access_key="made-up-key",
        config=Config(s3={"addressing_style": "path"}),
    )
    stubber = Stubber(client)
    used: list[tuple[str, str]] = []

    async def credentials() -> tuple[str, str]:
        return "made-up-id", "made-up-key"

    def factory(access: str, secret: str) -> Any:
        used.append((access, secret))
        return client

    return S3ObjectStore(_CONFIG, credentials=credentials, client_factory=factory), stubber, used


@pytest.mark.asyncio
async def test_s3_put_get_delete() -> None:
    store, stubber, used = _stubbed()
    stubber.add_response(
        "put_object",
        {},
        {"Bucket": "sessions", "Key": "k", "Body": b"data", "ContentType": "application/gzip"},
    )
    stubber.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"data"), 4)},
        {"Bucket": "sessions", "Key": "k"},
    )
    stubber.add_response(
        "delete_objects",
        {},
        {"Bucket": "sessions", "Delete": {"Objects": [{"Key": "k"}], "Quiet": True}},
    )
    with stubber:
        await store.put("k", b"data")
        assert await store.get("k") == b"data"
        await store.delete(["k"])
        await store.delete([])
    stubber.assert_no_pending_responses()
    assert used == [("made-up-id", "made-up-key")] * 3


@pytest.mark.asyncio
async def test_s3_errors_become_object_store_errors_without_secrets() -> None:
    store, stubber, _ = _stubbed()
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    stubber.add_response(
        "delete_objects", {"Errors": [{"Key": "k", "Code": "AccessDenied", "Message": "no"}]}
    )
    with stubber:
        with pytest.raises(ObjectStoreError, match="NoSuchKey") as exc:
            await store.get("k")
        assert "made-up-key" not in str(exc.value)
        with pytest.raises(ObjectStoreError, match="AccessDenied"):
            await store.delete(["k"])


@pytest.mark.asyncio
async def test_s3_missing_vault_keys_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_keys(api_path: str, *, check: str) -> dict[str, str] | None:
        return {"access_key_id": "made-up-id"}

    monkeypatch.setattr("meho_backplane.session_store.object_store.read_service_secret", no_keys)
    store = S3ObjectStore(_CONFIG)
    with pytest.raises(ObjectStoreError, match="secret_access_key"):
        await store.put("k", b"x")
