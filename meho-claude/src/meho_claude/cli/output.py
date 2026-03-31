"""Dual-mode output system: JSON for Claude Code, Rich for humans.

JSON mode: writes to stdout via msgspec (fast path).
Human mode: writes Rich-formatted output to stderr (keeps stdout clean).
Errors: always JSON to stdout with non-zero exit code.
"""

import sys
import time
from typing import Any

import msgspec
from rich.console import Console
from rich.table import Table

_encoder = msgspec.json.Encoder()
_console = Console(stderr=True)


def output_response(
    data: dict[str, Any],
    *,
    human: bool = False,
    start_time: float | None = None,
) -> None:
    """Unified output function for all CLI commands.

    JSON mode: writes to stdout (for Claude Code to parse).
    Human mode: writes Rich-formatted output to stderr (for terminal display).
    """
    if start_time is not None:
        data["duration_ms"] = round((time.time() - start_time) * 1000)

    if human:
        _output_rich(data)
    else:
        _output_json(data)


def _output_json(data: dict[str, Any]) -> None:
    """JSON to stdout -- Claude Code consumption path."""
    sys.stdout.buffer.write(_encoder.encode(data))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _output_rich(data: dict[str, Any]) -> None:
    """Rich-formatted output to stderr -- human debugging path."""
    if "error" in data:
        _console.print(f"[red bold]Error:[/] {data['error']}")
        if "code" in data:
            _console.print(f"[dim]Code:[/] {data['code']}")
        if "suggestion" in data:
            _console.print(f"[dim]Suggestion:[/] {data['suggestion']}")
        return

    # If data has list values containing dicts, render as Rich Table
    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            table = Table(title=key.replace("_", " ").title())
            for col in value[0].keys():
                table.add_column(col)
            for row in value:
                table.add_row(*[str(v) for v in row.values()])
            _console.print(table)
        else:
            _console.print(f"[bold]{key}:[/] {value}")


def output_error(
    error: str,
    *,
    code: str = "UNKNOWN_ERROR",
    suggestion: str | None = None,
    exit_code: int = 1,
) -> None:
    """Structured error output. Always JSON to stdout, always non-zero exit."""
    err: dict[str, Any] = {"status": "error", "error": error, "code": code}
    if suggestion:
        err["suggestion"] = suggestion
    sys.stdout.buffer.write(_encoder.encode(err))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    raise SystemExit(exit_code)
