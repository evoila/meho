# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Log sources that need no host and no cluster, for the shared log read tests."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from meho_backplane.connectors.kubernetes.logs_read_source import ContainerLog
from meho_backplane.connectors.kubernetes.ops_logs_window import parse_rfc3339

#: The time of journal entry 0, in microseconds and as a parameter.
T0 = 1_790_000_000_000_000
START = "2026-09-21T14:13:20Z"


def cursor(number: int) -> str:
    return f"s={'a' * 32};i={number:x};b={'b' * 32};m=1;t=2;x=3"


def journal_time(number: int) -> str:
    """The time of entry *number*, as a parameter of the read."""
    return datetime.fromtimestamp((T0 + number) / 1_000_000, UTC).isoformat()


def journal_line(number: int, message: Any) -> str:
    """What ``journalctl -o json`` prints for an entry; *number* is also its offset in time."""
    return json.dumps(
        {
            "MESSAGE": message,
            "__REALTIME_TIMESTAMP": str(T0 + number),
            "__CURSOR": cursor(number),
        }
    )


def proc(stdout: str, exit_status: int = 0) -> Any:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.exit_status = exit_status
    return result


def run_shell(cmd: str, stdin: str = "") -> str:
    done = subprocess.run(["sh", "-c", cmd], input=stdin.encode(), capture_output=True, check=True)
    return done.stdout.decode()


def _option(cmd: str, name: str) -> str | None:
    match = re.search(rf"--{name}=(?:'([^']*)'|(\S+))", cmd)
    return None if match is None else match.group(1) or match.group(2)


def _micros(value: str) -> int:
    moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f UTC").replace(tzinfo=UTC)
    return round(moment.timestamp() * 1_000_000)


class FakeJournal:
    """A host whose journal holds *messages*, one microsecond apart.

    The built command runs in a real shell. Only ``journalctl`` is made up.
    """

    def __init__(self, messages: list[Any]) -> None:
        self.entries = list(enumerate(messages))
        self.commands: list[str] = []

    def _select(self, cmd: str) -> list[tuple[int, Any]]:
        since, until = _option(cmd, "since"), _option(cmd, "until")
        entries = [
            (number, message)
            for number, message in self.entries
            if (since is None or T0 + number >= _micros(since))
            and (until is None or T0 + number <= _micros(until))
        ]
        if "--reverse" in cmd:
            entries.reverse()
        numbers = [cursor(number) for number, _ in entries]
        at, behind = _option(cmd, "cursor"), _option(cmd, "after-cursor")
        for marker, skip in ((at, 0), (behind, 1)):
            if marker in numbers:
                entries = entries[numbers.index(marker) + skip :]
        return entries

    async def run(self, _target: Any, cmd: str, **_: Any) -> Any:
        self.commands.append(cmd)
        text = "".join(journal_line(*entry) + "\n" for entry in self._select(cmd))
        return proc(run_shell("journalctl() { cat; }; " + cmd, text))


class FakePodLog:
    """A container log. *lines* start with their time; the node reads it like kubelet does."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.queries: list[dict[str, Any]] = []
        self.api_client = MagicMock()
        self.api_client.call_api = self._call_api

    async def _call_api(self, *_args: Any, **kwargs: Any) -> Any:
        query = dict(kwargs["query_params"])
        self.queries.append(query)
        lines = self.lines
        if "sinceTime" in query:
            since = parse_rfc3339(query["sinceTime"])
            lines = [line for line in lines if parse_rfc3339(line.split(" ", 1)[0]) >= since]
        if "tailLines" in query:
            lines = lines[-query["tailLines"] :]
        if not query.get("timestamps"):
            lines = [line.partition(" ")[2] for line in lines]
        body = "".join(f"{line}\n" for line in lines).encode()

        async def chunks(size: int) -> AsyncIterator[bytes]:
            for start in range(0, len(body), size):
                yield body[start : start + size]

        response = MagicMock(status=200, release=MagicMock(), read=AsyncMock(return_value=body))
        response.content.iter_chunked = chunks
        return response

    def patched(self) -> Any:
        """Let ``k8s.logs.read`` find this log."""
        log = ContainerLog(self.api_client, "p", "n", "c")
        return patch.object(ContainerLog, "find", AsyncMock(return_value=log))
