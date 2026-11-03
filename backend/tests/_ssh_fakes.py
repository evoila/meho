# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A stand-in for the ``asyncssh`` process the SSH adapter reads.

:meth:`SshConnector._run_command` opens a process with
``conn.create_process(cmd, input=..., encoding=None)``, reads its
``stdout`` and ``stderr`` as bytes and closes it. Tests that fake the
connection return a :class:`FakeSshProcess` from ``create_process`` so
the adapter's real read path runs against canned output.
"""

from __future__ import annotations

from types import TracebackType


class FakeSshStream:
    """Hands out canned bytes in the chunks the adapter asks for."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk, self._data = self._data, b""
        else:
            chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class FakeSshProcess:
    """One opened process with its canned output and exit status."""

    def __init__(
        self,
        *,
        command: str,
        stdout: str = "",
        stderr: str = "",
        exit_status: int | None = 0,
    ) -> None:
        self.env = None
        self.command = command
        self.subsystem = None
        self.exit_status = exit_status
        self.exit_signal = None
        self.returncode = exit_status
        self.stdout = FakeSshStream(stdout.encode("utf-8"))
        self.stderr = FakeSshStream(stderr.encode("utf-8"))

    async def __aenter__(self) -> FakeSshProcess:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False

    async def wait_closed(self) -> None:
        return None


class FakeSshConnection:
    """Stands in for an open ``asyncssh`` connection and records each command.

    ``calls`` holds one ``(cmd, stdin)`` pair per ``create_process`` call,
    so a test can check what reached the remote command line and what
    went to its standard input.
    """

    def __init__(self, *, stdout: str = "", stderr: str = "", exit_status: int | None = 0) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.closed = False
        self._stdout = stdout
        self._stderr = stderr
        self._exit_status = exit_status

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def create_process(self, cmd: str, **kwargs: object) -> FakeSshProcess:
        stdin = kwargs.get("input")
        self.calls.append((cmd, stdin if isinstance(stdin, str) else None))
        return FakeSshProcess(
            command=cmd,
            stdout=self._stdout,
            stderr=self._stderr,
            exit_status=self._exit_status,
        )
