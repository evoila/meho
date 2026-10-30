# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``k8s.logs`` time window (``since_time`` / ``until_time``).

The cluster side is a small stand-in for the node's log reader: it
keeps lines at or after ``sinceTime`` (whole seconds, as the API server
passes it on), applies ``tailLines``, and writes the RFC 3339 nanosecond
timestamp in front of every line. It answers with a streamed body of
raw bytes, chunk by chunk, as the real ``/log`` call does with
``_preload_content=False``, and counts how many bytes were read. The
tests drive ``k8s_logs`` against those recorded answers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import Draft202012Validator
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from meho_backplane.connectors.kubernetes.ops_logs import (
    K8S_LOGS_LLM_INSTRUCTIONS,
    K8S_LOGS_PARAMETER_SCHEMA,
    MAX_BODY_BYTES,
    MAX_TAIL_LINES,
    LogLineTooLargeError,
    LogWindowError,
    k8s_logs,
    parse_rfc3339,
)
from meho_backplane.connectors.kubernetes.ops_logs_window import (
    WINDOW_CHUNK_BYTES,
    LogWindow,
    collect_window,
    read_pod_log_window,
    window_query,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests.test_connectors_k8s_logs import (
    _TARGET,
    _make_connector,
    _make_operator,
    _pod_list,
    _pod_obj,
)

_POD = {"pod_name": "argocd-server", "namespace": "argocd"}


def _stamp(second: int, nanos: int = 0) -> str:
    """Kubernetes' fixed-width RFC 3339 form for 2026-08-01 + ``second``."""
    hours, rest = divmod(second, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"2026-08-01T{hours:02d}:{minutes:02d}:{seconds:02d}.{nanos:09d}Z"


def _raw_response(raw: bytes, status: int = 200, chunk_bytes: int | None = None) -> MagicMock:
    """A stand-in for the ``aiohttp`` response ``call_api`` returns unread.

    ``content.iter_chunked`` streams ``raw`` in pieces of ``chunk_bytes``
    (the size the reader asks for, unless set here) and records how
    many bytes were handed out in ``bytes_read``.
    """
    response = MagicMock(
        status=status,
        reason="OK" if status == 200 else "Not Found",
        headers={"Content-Type": "text/plain"},
        read=AsyncMock(return_value=raw),
        release=MagicMock(),
        bytes_read=0,
        body_bytes=len(raw),
    )

    def _iter_chunked(size: int) -> AsyncIterator[bytes]:
        step = chunk_bytes or size

        async def _chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(raw), step):
                piece = raw[start : start + step]
                response.bytes_read += len(piece)
                yield piece

        return _chunks()

    response.content.iter_chunked = _iter_chunked
    return response


def _kubelet_answer(
    entries: list[tuple[str, str]], api_client_mock: MagicMock, chunk_bytes: int | None
) -> Callable[..., Any]:
    """Build a ``call_api`` stand-in that answers like the node's log reader.

    The streamed response it hands out is kept as
    ``api_client_mock.last_response`` so a test can check how much of
    the body was read.
    """

    async def _answer(*_args: Any, **kwargs: Any) -> MagicMock:
        assert kwargs["_preload_content"] is False
        query = dict(kwargs["query_params"])
        assert query["timestamps"] is True
        assert "limitBytes" not in query
        since = query["sinceTime"].replace("Z", ".000000000Z")
        selected = [f"{ts} {text}\n" for ts, text in entries if ts >= since]
        if "tailLines" in query:
            selected = selected[-query["tailLines"] :]
        response = _raw_response("".join(selected).encode("utf-8"), chunk_bytes=chunk_bytes)
        api_client_mock.last_response = response
        return response

    return _answer


def _patch_cluster(
    entries: list[tuple[str, str]], chunk_bytes: int | None = None
) -> tuple[Any, Any, MagicMock, MagicMock]:
    api_client_mock = MagicMock(close=AsyncMock())
    api_client_mock.call_api = AsyncMock(
        side_effect=_kubelet_answer(entries, api_client_mock, chunk_bytes)
    )
    core_v1 = MagicMock()
    core_v1.list_namespaced_pod = AsyncMock(
        return_value=_pod_list([_pod_obj("argocd-server-7c4b8d-x7r2k", ["argocd-server"])])
    )
    core_v1.read_namespaced_pod_log = AsyncMock(return_value="newest\n")
    config_patch = patch(
        "meho_backplane.connectors.kubernetes.connector.config.new_client_from_config_dict",
        new_callable=AsyncMock,
        return_value=api_client_mock,
    )
    core_v1_patch = patch(
        "meho_backplane.connectors.kubernetes.ops_logs.client.CoreV1Api",
        return_value=core_v1,
    )
    return config_patch, core_v1_patch, core_v1, api_client_mock


async def _call(
    entries: list[tuple[str, str]], chunk_bytes: int | None = None, **params: Any
) -> tuple[dict[str, Any], MagicMock]:
    config_patch, core_v1_patch, _core_v1, api_client_mock = _patch_cluster(entries, chunk_bytes)
    with config_patch, core_v1_patch:
        result = await k8s_logs(_make_connector(), _TARGET, _make_operator(), {**_POD, **params})
    return result, api_client_mock


def _small_log() -> list[tuple[str, str]]:
    return [(_stamp(second), f"event {second}") for second in range(0, 100)]


def _big_log() -> list[tuple[str, str]]:
    # About 3 MiB. Lines come in pairs that share one timestamp, so the
    # paging check also covers a page that ends inside such a pair.
    entries = []
    for index in range(3000):
        stamp = _stamp(index // 20, (index // 2 % 10) * 100_000_000)
        entries.append((stamp, f"line {index:05d} " + "x" * 1000))
    return entries


def _kept_bytes(lines: list[str]) -> int:
    return sum(len(line.encode("utf-8")) for line in lines) + max(len(lines) - 1, 0)


def _texts(lines: list[str]) -> list[str]:
    return [line.split(" ", 1)[1] for line in lines]


async def _page_through(
    entries: list[tuple[str, str]], since_time: str, *, pages: int = 20
) -> list[str]:
    """Page with ``since_time = next_since_time`` until ``truncated`` is false.

    Checks on the way that every page stays under the cap, that each
    next page starts with a line at exactly ``next_since_time``, and
    that no page repeats a line of the page before.
    """
    collected: list[str] = []
    for _ in range(pages):
        result, _ = await _call(entries, since_time=since_time)
        page = result["lines"]
        assert page
        assert _kept_bytes(page) <= MAX_BODY_BYTES
        if collected:
            assert page[0].startswith(since_time + " ")
            assert not any(line.startswith(since_time + " ") for line in collected)
        collected.extend(page)
        if not result["truncated"]:
            return collected
        assert result["truncated_end"] == "end"
        since_time = result["next_since_time"]
    pytest.fail("paging did not reach the end of the log")


# ---------------------------------------------------------------------------
# parse_rfc3339 + schema
# ---------------------------------------------------------------------------


def test_parse_rfc3339_keeps_nanoseconds_and_offsets() -> None:
    assert parse_rfc3339("2026-08-01T00:00:00Z") == (1785542400, 0)
    assert parse_rfc3339("2026-08-01T00:00:00.000000001Z") == (1785542400, 1)
    assert parse_rfc3339("2026-08-01T02:00:00.5+02:00") == (1785542400, 500_000_000)


@pytest.mark.parametrize(
    "value", ["2026-08-01", "yesterday", "2026-08-01T00:00:00", "2026-13-01T00:00:00Z"]
)
def test_parse_rfc3339_refuses_other_forms(value: str) -> None:
    with pytest.raises(LogWindowError):
        parse_rfc3339(value)


def test_schema_accepts_the_window_params() -> None:
    validator = Draft202012Validator(K8S_LOGS_PARAMETER_SCHEMA)
    good = {**_POD, "since_time": "2026-08-01T00:00:00Z", "until_time": "2026-08-10T00:00:00+02:00"}
    assert list(validator.iter_errors(good)) == []
    assert list(validator.iter_errors({**_POD, "since_time": "1 August"})) != []


def test_guidance_names_the_limits() -> None:
    limits = K8S_LOGS_LLM_INSTRUCTIONS["limits"]
    assert str(MAX_TAIL_LINES) in limits
    assert "1 MiB" in limits
    assert "rotates" in limits
    assert "paging goes on with no gap" in limits
    assert "larger than 1 MiB cannot be returned" in limits
    assert "since_time" in K8S_LOGS_LLM_INSTRUCTIONS["parameter_hints"]
    assert "until_time" in K8S_LOGS_LLM_INSTRUCTIONS["parameter_hints"]


# ---------------------------------------------------------------------------
# Window reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_and_until_return_only_the_window() -> None:
    result, api_client_mock = await _call(
        _small_log(),
        since_time="2026-08-01T00:00:10Z",
        until_time="2026-08-01T00:00:19Z",
    )
    assert [line.split(" ", 1)[1] for line in result["lines"]] == [
        f"event {second}" for second in range(10, 20)
    ]
    assert result["lines"][0].startswith(_stamp(10))
    assert result["truncated"] is False
    assert "truncated_end" not in result

    kwargs = api_client_mock.call_api.call_args.kwargs
    query = dict(kwargs["query_params"])
    assert api_client_mock.call_api.call_args.args == (
        "/api/v1/namespaces/{namespace}/pods/{name}/log",
        "GET",
    )
    assert kwargs["path_params"] == {"name": "argocd-server-7c4b8d-x7r2k", "namespace": "argocd"}
    assert query["sinceTime"] == "2026-08-01T00:00:10Z"
    assert query["timestamps"] is True
    assert "limitBytes" not in query
    assert query["container"] == "argocd-server"
    # Forward read: no tail unless the caller set one.
    assert "tailLines" not in query


@pytest.mark.asyncio
async def test_since_time_inside_a_second_drops_the_earlier_lines() -> None:
    """The API server sends ``sinceTime`` in whole seconds; MEHO drops the rest."""
    entries = [(_stamp(5, nanos), f"n{nanos}") for nanos in (0, 250_000_000, 750_000_000)]
    result, api_client_mock = await _call(entries, since_time="2026-08-01T00:00:05.25Z")
    assert dict(api_client_mock.call_api.call_args.kwargs["query_params"])["sinceTime"] == (
        "2026-08-01T00:00:05Z"
    )
    assert [line.split(" ", 1)[1] for line in result["lines"]] == ["n250000000", "n750000000"]


@pytest.mark.asyncio
async def test_since_time_with_offset_is_sent_in_utc() -> None:
    _result, api_client_mock = await _call(_small_log(), since_time="2026-08-01T02:00:10+02:00")
    query = dict(api_client_mock.call_api.call_args.kwargs["query_params"])
    assert query["sinceTime"] == "2026-08-01T00:00:10Z"


@pytest.mark.asyncio
async def test_tail_with_since_time_is_sent() -> None:
    result, api_client_mock = await _call(_small_log(), since_time="2026-08-01T00:00:00Z", tail=5)
    assert dict(api_client_mock.call_api.call_args.kwargs["query_params"])["tailLines"] == 5
    assert result["lines"][-1].endswith("event 99")


@pytest.mark.asyncio
async def test_large_window_keeps_the_oldest_lines_and_says_where_it_was_cut() -> None:
    entries = _big_log()
    result, _ = await _call(entries, since_time="2026-08-01T00:00:00Z")

    assert result["truncated"] is True
    assert result["truncated_end"] == "end"
    assert result["lines"][0] == f"{entries[0][0]} {entries[0][1]}"
    assert _kept_bytes(result["lines"]) <= MAX_BODY_BYTES
    assert _kept_bytes(result["lines"]) > MAX_BODY_BYTES - 3000
    assert result["line_count"] == len(result["lines"])
    # The cut is at a time boundary: the next line to come has a later
    # time than the last line kept, and the answer names that time.
    kept = len(result["lines"])
    assert result["lines"] == [f"{ts} {text}" for ts, text in entries[:kept]]
    assert entries[kept][0] > entries[kept - 1][0]
    assert result["next_since_time"] == entries[kept][0]
    assert all(line.endswith("x" * 1000) for line in result["lines"])


@pytest.mark.asyncio
async def test_paging_from_next_since_time_has_no_gap() -> None:
    entries = _big_log()
    collected = await _page_through(entries, "2026-08-01T00:00:00Z")
    assert collected == [f"{ts} {text}" for ts, text in entries]


@pytest.mark.asyncio
async def test_paging_reaches_every_line_when_one_second_holds_more_than_the_cap() -> None:
    """``sinceTime`` goes out in whole seconds; paging still moves on.

    Each page asks Kubernetes for the log from the start of the second
    of ``next_since_time``. When that second holds more than one page,
    the lines before ``next_since_time`` are read and thrown away as
    they arrive, so every page carries new lines and the read reaches
    the end. About 2 MiB are written within one second here.
    """
    entries = [
        (_stamp(0, index * 1000), f"line {index:05d} " + "x" * 1000) for index in range(2200)
    ]
    assert sum(len(f"{ts} {text}\n") for ts, text in entries) > 2 * MAX_BODY_BYTES

    collected = await _page_through(entries, "2026-08-01T00:00:00Z", pages=4)
    assert collected == [f"{ts} {text}" for ts, text in entries]


@pytest.mark.asyncio
async def test_a_full_page_stops_reading_the_body() -> None:
    """The read stops soon after the page is full; the rest is never loaded."""
    _result, api_client_mock = await _call(_big_log(), since_time="2026-08-01T00:00:00Z")
    response = api_client_mock.last_response
    assert response.body_bytes > 2 * MAX_BODY_BYTES
    assert response.bytes_read <= MAX_BODY_BYTES + 2 * WINDOW_CHUNK_BYTES
    response.release.assert_called_once()


@pytest.mark.asyncio
async def test_a_line_that_does_not_fit_opens_the_next_page() -> None:
    """A big line that does not fit behind the earlier lines is not lost.

    The next page starts with that line: ``next_since_time`` is its
    time, and nothing from the first page comes again.
    """
    half = "h" * (MAX_BODY_BYTES // 2)
    entries = [(_stamp(1), half), (_stamp(2), half), (_stamp(3), "tail")]
    first, _ = await _call(entries, since_time="2026-08-01T00:00:00Z")
    assert _texts(first["lines"]) == [half]
    assert first["truncated"] is True
    assert first["next_since_time"] == _stamp(2)

    second, _ = await _call(entries, since_time=_stamp(2))
    assert _texts(second["lines"]) == [half, "tail"]
    assert second["truncated"] is False

    collected = await _page_through(entries, "2026-08-01T00:00:00Z")
    assert collected == [f"{ts} {text}" for ts, text in entries]


@pytest.mark.asyncio
async def test_a_line_of_exactly_the_cap_is_reached_by_paging() -> None:
    """The boundary: a timestamped line of exactly ``MAX_BODY_BYTES``.

    It fits a page only on its own. The page before ends in front of
    it, the next page holds just this line, and the page after that
    goes on behind it. Paging reaches every line.
    """
    prefix = f"{_stamp(2)} "
    exact = "e" * (MAX_BODY_BYTES - len(prefix))
    entries = [(_stamp(1), "before"), (_stamp(2), exact), (_stamp(3), "after")]
    assert len(f"{_stamp(2)} {exact}".encode()) == MAX_BODY_BYTES

    first, _ = await _call(entries, since_time="2026-08-01T00:00:00Z")
    assert _texts(first["lines"]) == ["before"]
    assert first["next_since_time"] == _stamp(2)

    second, _ = await _call(entries, since_time=_stamp(2))
    assert _texts(second["lines"]) == [exact]
    assert second["truncated"] is True
    assert second["next_since_time"] == _stamp(3)

    third, _ = await _call(entries, since_time=_stamp(3))
    assert _texts(third["lines"]) == ["after"]
    assert third["truncated"] is False

    collected = await _page_through(entries, "2026-08-01T00:00:00Z")
    assert collected == [f"{ts} {text}" for ts, text in entries]


@pytest.mark.asyncio
async def test_a_page_never_ends_between_lines_with_the_same_time() -> None:
    """A cut inside a group of lines with one timestamp moves back to its start.

    The lines of the group that were already kept come again with the
    next page, so ``since_time = next_since_time`` repeats nothing and
    skips nothing.
    """
    # Three such lines, with their timestamps, fit in one page together.
    third = "t" * (MAX_BODY_BYTES // 3 - 100)
    entries = [
        (_stamp(1), third),
        (_stamp(2), third),
        (_stamp(2), third),
        (_stamp(2), third),
        (_stamp(3), "after"),
    ]
    first, _ = await _call(entries, since_time="2026-08-01T00:00:00Z")
    assert _texts(first["lines"]) == [third]
    assert first["truncated"] is True
    assert first["next_since_time"] == _stamp(2)

    second, _ = await _call(entries, since_time=_stamp(2))
    assert _texts(second["lines"]) == [third, third, third, "after"]
    assert second["truncated"] is False

    collected = await _page_through(entries, "2026-08-01T00:00:00Z")
    assert collected == [f"{ts} {text}" for ts, text in entries]


@pytest.mark.asyncio
async def test_lines_with_one_timestamp_that_exceed_the_cap_are_a_plain_error() -> None:
    """Eight lines of 512 KiB at the start time: the page stays bounded.

    They can never be returned together, and a page cannot end between
    them, so the call fails like it does for one oversized line. The
    read stops early.
    """
    # Two such lines, with their timestamps, fit in one page; three do not.
    big = "b" * (512 * 1024 - 100)
    entries = [(_stamp(1), big) for _ in range(8)] + [(_stamp(2), "after")]
    config_patch, core_v1_patch, _core_v1, api_client_mock = _patch_cluster(entries)
    with config_patch, core_v1_patch, pytest.raises(LogLineTooLargeError) as caught:
        await k8s_logs(
            _make_connector(), _TARGET, _make_operator(), {**_POD, "since_time": _stamp(1)}
        )
    assert caught.value.line_time == _stamp(1)
    assert caught.value.line_count == 3
    assert "3 log lines at " + _stamp(1) in str(caught.value)
    assert "together larger than 1 MiB" in str(caught.value)
    # Read: the two kept lines and the third one that did not fit.
    response = api_client_mock.last_response
    assert response.body_bytes > 3 * MAX_BODY_BYTES
    assert response.bytes_read <= 2 * MAX_BODY_BYTES
    response.release.assert_called_once()

    # The caller continues behind that time.
    result, _ = await _call(entries, since_time=_stamp(1, 1))
    assert _texts(result["lines"]) == ["after"]


@pytest.mark.asyncio
async def test_too_large_group_behind_earlier_lines_ends_the_page_before_it() -> None:
    """The earlier lines are returned; the error comes only when the group is next."""
    big = "b" * (600 * 1024)
    entries = [(_stamp(1), "before"), (_stamp(2), big), (_stamp(2), big), (_stamp(3), "after")]
    first, _ = await _call(entries, since_time="2026-08-01T00:00:00Z")
    assert _texts(first["lines"]) == ["before"]
    assert first["truncated"] is True
    assert first["next_since_time"] == _stamp(2)

    with pytest.raises(LogLineTooLargeError) as caught:
        await _call(entries, since_time=_stamp(2))
    assert caught.value.line_time == _stamp(2)
    assert caught.value.line_count == 2


@pytest.mark.asyncio
async def test_window_that_ends_before_the_byte_limit_is_not_cut() -> None:
    """``until_time`` reached inside the bytes Kubernetes sent: the window is whole."""
    entries = _big_log()
    result, _ = await _call(
        entries,
        since_time="2026-08-01T00:00:00Z",
        until_time="2026-08-01T00:00:04.9Z",
    )
    assert result["truncated"] is False
    assert len(result["lines"]) == 100
    assert "next_since_time" not in result


@pytest.mark.asyncio
async def test_window_result_goes_behind_a_result_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    result, _ = await _call(_big_log(), since_time="2026-08-01T00:00:00Z")
    context = {"op_id": "k8s.logs", "result_ordering": K8S_LOGS_LLM_INSTRUCTIONS["result_ordering"]}
    reduced, handle = await JsonFluxReducer().reduce(result, None, context)
    assert handle is not None
    assert handle.total_rows == result["line_count"]
    assert reduced["truncated"] is True
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Refused combinations and unchanged default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"since": "7d", "since_time": "2026-08-01T00:00:00Z"}, "not both"),
        ({"until_time": "2026-08-10T00:00:00Z"}, "needs 'since_time'"),
        (
            {"since_time": "2026-08-10T00:00:00Z", "until_time": "2026-08-01T00:00:00Z"},
            "before",
        ),
        ({"since_time": "2026-02-30T00:00:00Z"}, "not a valid time"),
    ],
)
async def test_unusable_window_params_are_refused_before_any_cluster_call(
    params: dict[str, Any], message: str
) -> None:
    config_patch, core_v1_patch, core_v1, api_client_mock = _patch_cluster(_small_log())
    with config_patch, core_v1_patch, pytest.raises(LogWindowError, match=message):
        await k8s_logs(_make_connector(), _TARGET, _make_operator(), {**_POD, **params})
    core_v1.list_namespaced_pod.assert_not_awaited()
    api_client_mock.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_window_params_the_newest_lines_path_is_unchanged() -> None:
    config_patch, core_v1_patch, core_v1, api_client_mock = _patch_cluster(_small_log())
    with config_patch, core_v1_patch:
        result = await k8s_logs(_make_connector(), _TARGET, _make_operator(), dict(_POD))
    assert result == {
        "pod": "argocd-server-7c4b8d-x7r2k",
        "namespace": "argocd",
        "container": "argocd-server",
        "lines": ["newest"],
        "truncated": False,
    }
    assert core_v1.read_namespaced_pod_log.call_args.kwargs == {
        "name": "argocd-server-7c4b8d-x7r2k",
        "namespace": "argocd",
        "container": "argocd-server",
        "tail_lines": 100,
        "previous": False,
    }
    api_client_mock.call_api.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wire shape through the real kubernetes_asyncio ApiClient
# ---------------------------------------------------------------------------

_FROM_START = LogWindow(since=(1785542400, 5), until=None)


async def _read_through_real_api_client(response: MagicMock) -> tuple[Any, AsyncMock]:
    """Run :func:`read_pod_log_window` through a real ``ApiClient``.

    Only the HTTP layer (``ApiClient.request``) is replaced, so the SDK's
    own handling of the answer runs.
    """
    configuration = client.Configuration(host="https://k8s.test:6443")
    api_client = client.ApiClient(configuration)
    try:
        with patch.object(api_client, "request", AsyncMock(return_value=response)) as request:
            query = window_query(
                _FROM_START, container="argocd-server", previous=False, tail_lines=None
            )
            page = await read_pod_log_window(
                api_client,
                name="argocd-server-7c4b8d-x7r2k",
                namespace="argocd",
                query=query,
                window=_FROM_START,
                cap_bytes=MAX_BODY_BYTES,
            )
    finally:
        await api_client.close()
    return page, request


@pytest.mark.asyncio
async def test_window_request_on_the_wire() -> None:
    """The real ``ApiClient`` builds the URL and query and streams the body."""
    response = _raw_response(f"{_stamp(1)} hello\n".encode())
    page, request = await _read_through_real_api_client(response)

    assert page.lines == [f"{_stamp(1)} hello"]
    assert page.truncated is False
    assert page.next_time is None
    response.release.assert_called_once()
    args = request.call_args
    assert args.args == (
        "GET",
        "https://k8s.test:6443/api/v1/namespaces/argocd/pods/argocd-server-7c4b8d-x7r2k/log",
    )
    assert args.kwargs["_preload_content"] is False
    assert args.kwargs["query_params"] == [
        ("container", "argocd-server"),
        ("previous", False),
        ("sinceTime", "2026-08-01T00:00:00Z"),
        ("timestamps", True),
    ]


@pytest.mark.asyncio
async def test_window_read_raises_api_exception_on_an_error_status() -> None:
    """The raw read does not let an error answer pass as a log body."""
    response = _raw_response(b'{"kind":"Status","reason":"NotFound"}', status=404)
    with pytest.raises(ApiException) as caught:
        await _read_through_real_api_client(response)
    assert caught.value.status == 404
    assert caught.value.body == '{"kind":"Status","reason":"NotFound"}'
    response.release.assert_called_once()


# ---------------------------------------------------------------------------
# Chunk boundaries and oversized lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lines_and_characters_split_across_chunks_are_whole() -> None:
    """Through ``k8s_logs``: odd chunk sizes never cut a line or a character."""
    entries = [(_stamp(index // 10, index % 10), "ü" * 1000) for index in range(1000)]
    result, _ = await _call(entries, chunk_bytes=777, since_time="2026-08-01T00:00:00Z")
    assert result["truncated"] is True
    assert result["lines"]
    assert all(line.endswith("ü" * 1000) for line in result["lines"])
    assert not any("\ufffd" in line for line in result["lines"])
    assert result["lines"] == [f"{ts} {text}" for ts, text in entries[: len(result["lines"])]]


@pytest.mark.asyncio
async def test_last_line_without_a_newline_is_kept() -> None:
    async def _chunks() -> AsyncIterator[bytes]:
        yield f"{_stamp(1)} first\n{_stamp(2)} sec".encode()
        yield b"ond"

    page = await collect_window(_chunks(), _FROM_START, cap_bytes=MAX_BODY_BYTES)
    assert page.lines == [f"{_stamp(1)} first", f"{_stamp(2)} second"]
    assert page.truncated is False


@pytest.mark.asyncio
async def test_oversized_line_inside_the_window_is_a_plain_error() -> None:
    """A line over the cap can never be returned, so the call says so and stops.

    The lines before it are still returned: the page before ends in
    front of it, and the error comes when the next call starts at it.
    """
    entries = [
        (_stamp(1), "before"),
        (_stamp(2), "o" * (MAX_BODY_BYTES + 10)),
        (_stamp(3), "after"),
    ]
    first, _ = await _call(entries, since_time="2026-08-01T00:00:00Z")
    assert _texts(first["lines"]) == ["before"]
    assert first["truncated"] is True
    assert first["next_since_time"] == _stamp(2)

    with pytest.raises(LogLineTooLargeError) as caught:
        await _call(entries, since_time=_stamp(2))
    assert caught.value.line_time == _stamp(2)
    assert caught.value.line_count == 1
    assert "the log line at " + _stamp(2) + " is larger than 1 MiB" in str(caught.value)
    assert "since_time" in str(caught.value)


@pytest.mark.asyncio
async def test_oversized_line_error_stops_the_read_early() -> None:
    entries = [(_stamp(2), "o" * (4 * MAX_BODY_BYTES)), (_stamp(3), "after")]
    config_patch, core_v1_patch, _core_v1, api_client_mock = _patch_cluster(entries)
    with config_patch, core_v1_patch, pytest.raises(LogLineTooLargeError):
        await k8s_logs(
            _make_connector(),
            _TARGET,
            _make_operator(),
            {**_POD, "since_time": "2026-08-01T00:00:00Z"},
        )
    response = api_client_mock.last_response
    assert response.bytes_read <= MAX_BODY_BYTES + 2 * WINDOW_CHUNK_BYTES
    response.release.assert_called_once()


@pytest.mark.asyncio
async def test_oversized_line_before_the_window_is_skipped_without_being_stored() -> None:
    """The caller continues behind an oversized line with a later ``since_time``."""
    entries = [
        (_stamp(2, 100), "o" * (3 * MAX_BODY_BYTES)),
        (_stamp(2, 200), "after"),
        (_stamp(3), "later"),
    ]
    result, _ = await _call(entries, since_time=_stamp(2, 101))
    assert _texts(result["lines"]) == ["after", "later"]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_oversized_line_behind_the_window_ends_the_read() -> None:
    entries = [
        (_stamp(1), "inside"),
        (_stamp(5), "o" * (3 * MAX_BODY_BYTES)),
    ]
    result, api_client_mock = await _call(
        entries, since_time="2026-08-01T00:00:00Z", until_time=_stamp(2)
    )
    assert _texts(result["lines"]) == ["inside"]
    assert result["truncated"] is False
    response = api_client_mock.last_response
    assert response.bytes_read < response.body_bytes
