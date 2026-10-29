# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``k8s.logs`` time window (``since_time`` / ``until_time``).

The cluster side is a small stand-in for the node's log reader: it
keeps lines at or after ``sinceTime`` (whole seconds, as the API server
passes it on), applies ``tailLines``, writes the RFC 3339 nanosecond
timestamp in front of every line, and stops after ``limitBytes`` even
in the middle of a line. The tests drive ``k8s_logs`` against those
recorded answers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jsonschema import Draft202012Validator
from kubernetes_asyncio import client

from meho_backplane.connectors.kubernetes.ops_logs import (
    K8S_LOGS_LLM_INSTRUCTIONS,
    K8S_LOGS_PARAMETER_SCHEMA,
    MAX_BODY_BYTES,
    MAX_TAIL_LINES,
    WINDOW_LIMIT_BYTES,
    LogWindowError,
    k8s_logs,
    parse_rfc3339,
)
from meho_backplane.connectors.kubernetes.ops_logs_window import (
    LogWindow,
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


def _kubelet_answer(entries: list[tuple[str, str]]) -> Callable[..., Any]:
    """Build a ``call_api`` stand-in that answers like the node's log reader."""

    async def _answer(*_args: Any, **kwargs: Any) -> str:
        query = dict(kwargs["query_params"])
        assert query["timestamps"] is True
        since = query["sinceTime"].replace("Z", ".000000000Z")
        selected = [f"{ts} {text}\n" for ts, text in entries if ts >= since]
        if "tailLines" in query:
            selected = selected[-query["tailLines"] :]
        raw = "".join(selected).encode("utf-8")[: query["limitBytes"]]
        return raw.decode("utf-8", errors="ignore")

    return _answer


def _patch_cluster(entries: list[tuple[str, str]]) -> tuple[Any, Any, MagicMock, MagicMock]:
    api_client_mock = MagicMock(close=AsyncMock())
    api_client_mock.call_api = AsyncMock(side_effect=_kubelet_answer(entries))
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


async def _call(entries: list[tuple[str, str]], **params: Any) -> tuple[dict[str, Any], MagicMock]:
    config_patch, core_v1_patch, _core_v1, api_client_mock = _patch_cluster(entries)
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
    assert query["limitBytes"] == WINDOW_LIMIT_BYTES
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
    assert result["line_count"] == len(result["lines"])
    last = result["lines"][-1]
    assert result["last_line_time"] == last.split(" ", 1)[0]
    # Every kept line is whole: no line cut in the middle by limitBytes.
    assert all(line.endswith("x" * 1000) for line in result["lines"])


@pytest.mark.asyncio
async def test_paging_from_last_line_time_has_no_gap() -> None:
    entries = _big_log()
    expected = [f"{ts} {text}" for ts, text in entries]

    collected: list[str] = []
    since_time = "2026-08-01T00:00:00Z"
    for _ in range(10):
        result, _ = await _call(entries, since_time=since_time)
        page = result["lines"]
        if collected:
            # The next page starts again at the last kept time: the
            # lines it repeats are exactly the kept lines with that time.
            repeated = [line for line in collected if line.startswith(since_time + " ")]
            assert repeated
            assert page[: len(repeated)] == repeated
            page = page[len(repeated) :]
        collected.extend(page)
        if not result["truncated"]:
            break
        since_time = result["last_line_time"]
    else:
        pytest.fail("paging did not reach the end of the log")

    assert collected == expected


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
    assert "last_line_time" not in result


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


@pytest.mark.asyncio
async def test_window_request_on_the_wire() -> None:
    """The real ``ApiClient`` builds the URL and query and returns the text body."""
    configuration = client.Configuration(host="https://k8s.test:6443")
    api_client = client.ApiClient(configuration)
    response = MagicMock(status=200, data=f"{_stamp(1)} hello\n".encode())
    response.getheader.return_value = "text/plain; charset=utf-8"
    try:
        with patch.object(api_client, "request", AsyncMock(return_value=response)) as request:
            query = window_query(
                LogWindow(since=(1785542400, 5), until=None),
                container="argocd-server",
                previous=False,
                tail_lines=None,
                limit_bytes=WINDOW_LIMIT_BYTES,
            )
            body = await read_pod_log_window(
                api_client,
                name="argocd-server-7c4b8d-x7r2k",
                namespace="argocd",
                query=query,
            )
    finally:
        await api_client.close()

    assert body == f"{_stamp(1)} hello\n"
    args = request.call_args
    assert args.args == (
        "GET",
        "https://k8s.test:6443/api/v1/namespaces/argocd/pods/argocd-server-7c4b8d-x7r2k/log",
    )
    assert args.kwargs["query_params"] == [
        ("container", "argocd-server"),
        ("previous", False),
        ("sinceTime", "2026-08-01T00:00:00Z"),
        ("timestamps", True),
        ("limitBytes", WINDOW_LIMIT_BYTES),
    ]
