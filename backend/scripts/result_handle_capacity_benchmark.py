# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Capacity benchmark for stored result handles (#3642).

Measures what large vendor results cost on the result-delivery path, and
turns the numbers into recommended values for four limits:

* the result-cache Valkey ``maxmemory`` (chart ``resultCache.maxmemory``);
* the reducer's work guard on the decoded input: decoded bytes and nodes
  (``RESULT_REDUCTION_MAX_DECODED_BYTES`` / ``RESULT_REDUCTION_MAX_NODES``);
* the wire ceiling for a completed ``call_operation`` response
  (``RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES``);
* the wire ceiling for a ``result_query`` response
  (``RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES``).

It runs in one process, with no Valkey and no network. Each payload
profile is built from rows shaped like a real vendor list response. The
payload goes through the real reducer and the real store codec into an
in-memory client, and the stored record is read back the way
``result_query`` reads it. Wire sizes come from the same encoder the MCP
transport uses (:func:`measure_delivered_wire_bytes`).

Split, like :mod:`scripts.soak_harness`:

* a deterministic core (row builders, the work count, the allocation
  estimate, the recommendation rules, the table) that the unit tests pin;
* a measuring layer (:func:`measure_profile`) that runs the real code.
  Decode time and heap depend on the machine and are reported only.

Usage::

    cd backend
    uv run python scripts/result_handle_capacity_benchmark.py
    uv run python scripts/result_handle_capacity_benchmark.py --rows 2000 --format json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import tracemalloc
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import msgspec
import structlog

from meho_backplane.connectors.result_handle_store import ResultHandleStore, _StoredPayload
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.settings import Settings

if __package__ in (None, ""):
    # Run as a file (``python scripts/...py``): make ``scripts`` importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.result_handle_capacity_profiles import (
    PROFILES,
    PayloadProfile,
    reduce_into_store,
)

__all__ = [
    "PROFILES",
    "AdmissionWork",
    "PayloadProfile",
    "ProfileMeasurement",
    "Recommendation",
    "derive_recommendations",
    "main",
    "measure_admission_work",
    "measure_profile",
    "render_markdown",
    "round_up",
    "valkey_allocation_bytes",
]

KIB = 1024
MIB = 1024 * KIB

#: Chart default for ``resultCache.maxmemory`` (``768mb``; Valkey reads
#: ``mb`` as 1024 * 1024 bytes). Source: deploy/charts/meho/values.yaml.
CHART_MAXMEMORY_BYTES = 768 * MIB
#: Memory an empty Valkey uses before it holds any handle (rounded up).
VALKEY_BASELINE_BYTES = 16 * MIB
#: Valkey string header plus the trailing NUL, added to a stored value.
_SDS_OVERHEAD_BYTES = 16
#: Byte budget for the inline sample, as in the reducer's setting default.
_SAMPLE_BYTE_BUDGET = 4096


def _default(name: str) -> Any:
    """Return a :class:`Settings` field default without loading the env."""
    return Settings.model_fields[name].default


# ---------------------------------------------------------------------------
# Deterministic core
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionWork:
    """The work the reducer's guard counts for one payload."""

    nodes: int
    decoded_bytes: int
    depth: int


def _scalar_bytes(value: Any) -> int:
    """Bytes the guard charges for one scalar (same rules as the guard)."""
    if value is None:
        return 4
    if isinstance(value, bool):
        return 5
    if isinstance(value, int):
        digits = max(1, (value.bit_length() * 30_103 + 99_999) // 100_000)
        return digits + (1 if value < 0 else 0)
    if isinstance(value, float):
        return 24
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    raise TypeError(f"not a JSON value: {type(value).__name__}")


def measure_admission_work(value: Any) -> AdmissionWork:
    """Count nodes, decoded bytes and depth the way :class:`AdmissionGuard` does.

    The guard only answers yes or no. This returns the counts, so the
    smallest limits that admit *value* are exactly these numbers.
    """
    nodes = size = depth = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, level = stack.pop()
        nodes += 1
        depth = max(depth, level)
        if isinstance(item, dict):
            size += 2
            for key, child in item.items():
                nodes += 1
                size += len(key.encode("utf-8"))
                stack.append((child, level + 1))
        elif isinstance(item, list):
            size += 2
            stack.extend((child, level + 1) for child in item)
        else:
            size += _scalar_bytes(item)
    return AdmissionWork(nodes=nodes, decoded_bytes=size, depth=depth)


def valkey_allocation_bytes(value_bytes: int) -> int:
    """Estimate the memory Valkey uses to hold one string of *value_bytes*.

    Adds the string header, then rounds up to the jemalloc size class:
    four classes per doubling from 16 KiB up (jemalloc(3), "Size classes").
    Smaller values round to 16 bytes. A jemalloc build that rounds large
    allocations to pages instead gives a smaller number, so this is the
    upper estimate.
    """
    n = value_bytes + _SDS_OVERHEAD_BYTES
    if n <= 16 * KIB:
        return round_up(n, 16)
    group = 1 << ((n - 1).bit_length() - 1)
    return round_up(n, group // 4)


def round_up(value: float, step: int) -> int:
    """Round *value* up to a whole multiple of *step*."""
    return math.ceil(value / step) * step


@dataclass(frozen=True, slots=True)
class ProfileMeasurement:
    """What one profile cost on the delivery path.

    ``nodes`` / ``decoded_bytes`` / ``depth`` are the guard's work for the
    rows the store kept (``stored_rows``), not for every captured row.
    """

    profile: str
    captured_rows: int
    stored_rows: int
    record_bytes: int
    cache_bytes: int
    nodes: int
    decoded_bytes: int
    depth: int
    page_rows: int
    page_wire_bytes: int
    complete_wire_bytes: int
    decode_heap_bytes: int
    decode_ms: float


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A recommended value for one limit, next to its current default."""

    setting: str
    current: int
    needed: int
    recommended: int
    action: str
    basis: str


def _action(current: int, recommended: int) -> str:
    """``raise`` below the recommendation, ``lower`` above twice it, else ``keep``."""
    if current < recommended:
        return "raise"
    if current > 2 * recommended:
        return "lower"
    return "keep"


def _rec(setting: str, current: int, needed: int, recommended: int, basis: str) -> Recommendation:
    return Recommendation(
        setting, current, needed, recommended, _action(current, recommended), basis
    )


def derive_recommendations(
    measurements: Sequence[ProfileMeasurement],
    *,
    full_size_handles: int,
    headroom: float,
    max_record_bytes: int,
) -> list[Recommendation]:
    """Turn measurements into recommended limits (pure; no I/O).

    * Cache ``maxmemory``: room for *full_size_handles* records at the
      per-record cap, plus the empty-Valkey baseline, in 64 MiB steps.
      ``noeviction`` refuses writes at the ceiling, so it is sized for the
      worst case, not the typical record.
    * Decoded bytes and nodes: the guard should admit any payload the
      store could still keep whole. So each profile's work per stored
      byte is scaled up to a record that fills the per-record cap; the
      largest, times *headroom*, in 16 MiB and 250k steps. This does not
      depend on how many rows were measured.
    * Wire ceilings: the largest response measured, times *headroom*, in
      256 KiB (query) and 64 KiB (completed call) steps. The completed
      call never drops below the 2048-byte receipt floor.
    """
    if not measurements:
        raise ValueError("no measurements")
    cap_cache = valkey_allocation_bytes(max_record_bytes)
    cache_needed = full_size_handles * cap_cache + VALKEY_BASELINE_BYTES
    stored = [m for m in measurements if m.record_bytes > 0]
    if not stored:
        raise ValueError("no profile stored a record")
    decoded = max(math.ceil(m.decoded_bytes / m.record_bytes * max_record_bytes) for m in stored)
    nodes = max(math.ceil(m.nodes / m.record_bytes * max_record_bytes) for m in stored)
    page = max(m.page_wire_bytes for m in measurements)
    complete = max(m.complete_wire_bytes for m in measurements)
    return [
        _rec(
            "resultCache.maxmemory",
            CHART_MAXMEMORY_BYTES,
            cache_needed,
            round_up(cache_needed, 64 * MIB),
            f"{full_size_handles} records at the cap ({cap_cache} bytes each in Valkey) + baseline",
        ),
        _rec(
            "RESULT_REDUCTION_MAX_DECODED_BYTES",
            _default("result_reduction_max_decoded_bytes"),
            decoded,
            round_up(decoded * headroom, 16 * MIB),
            f"payload that fills the {max_record_bytes}-byte record cap x {headroom}",
        ),
        _rec(
            "RESULT_REDUCTION_MAX_NODES",
            _default("result_reduction_max_nodes"),
            nodes,
            round_up(nodes * headroom, 250_000),
            f"payload that fills the {max_record_bytes}-byte record cap x {headroom}",
        ),
        _rec(
            "RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES",
            _default("result_complete_response_wire_max_bytes"),
            complete,
            max(2048, round_up(complete * headroom, 64 * KIB)),
            f"largest completed response x {headroom}",
        ),
        _rec(
            "RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES",
            _default("result_query_response_wire_max_bytes"),
            page,
            round_up(page * headroom, 256 * KIB),
            f"largest full read page x {headroom}",
        ),
    ]


def render_markdown(
    measurements: Sequence[ProfileMeasurement],
    recommendations: Sequence[Recommendation],
    *,
    workers: int,
    concurrent_peak_bytes: int,
) -> str:
    """Render the results as two Markdown tables plus one summary line."""
    lines = [
        "| Setting | Current default | Needed | Recommended | Action | Basis |",
        "|---|---:|---:|---:|---|---|",
    ]
    for r in recommendations:
        lines.append(
            f"| `{r.setting}` | {r.current:,} | {r.needed:,} | {r.recommended:,} "
            f"| {r.action} | {r.basis} |"
        )
    lines += [
        "",
        "| Profile | Rows stored | Record bytes | Cache bytes | Nodes | Decoded bytes "
        "| Depth | Page rows | Page wire | Completed wire | Decode heap | Decode ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for m in measurements:
        lines.append(
            f"| {m.profile} | {m.stored_rows:,} / {m.captured_rows:,} | {m.record_bytes:,} "
            f"| {m.cache_bytes:,} | {m.nodes:,} | {m.decoded_bytes:,} | {m.depth} "
            f"| {m.page_rows} | {m.page_wire_bytes:,} | {m.complete_wire_bytes:,} "
            f"| {m.decode_heap_bytes:,} | {m.decode_ms:.0f} |"
        )
    lines += [
        "",
        f"Peak heap for {workers} parallel decodes of the largest record: "
        f"{concurrent_peak_bytes:,} bytes.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Measuring layer (runs the real reducer, store codec and wire encoder)
# ---------------------------------------------------------------------------


class _MemoryClient:
    """Dict-backed async Valkey stand-in: ``get`` and ``set`` only."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


class _CappedStore(ResultHandleStore):
    """The real store with an explicit record cap, so no settings are read."""

    def __init__(self, client: _MemoryClient, max_record_bytes: int) -> None:
        super().__init__(client)  # type: ignore[arg-type]
        self._cap = max_record_bytes

    async def spill(self, **kwargs: Any) -> Any:
        kwargs.setdefault("max_record_bytes", self._cap)
        return await super().spill(**kwargs)

    async def spill_document(self, **kwargs: Any) -> Any:
        kwargs.setdefault("max_record_bytes", self._cap)
        return await super().spill_document(**kwargs)


def _largest_page(rows: list[dict[str, Any]], max_rows: int, max_bytes: int) -> list[Any]:
    """The largest page ``result_query`` still returns: at most *max_rows* rows
    whose encoded JSON fits *max_bytes* (binary search on the prefix length)."""
    low, high = 0, min(len(rows), max_rows)
    while low < high:
        mid = (low + high + 1) // 2
        if len(msgspec.json.encode(rows[:mid])) <= max_bytes:
            low = mid
        else:
            high = mid - 1
    return rows[:low]


def _decode_cost(raw: bytes) -> tuple[_StoredPayload, int, float]:
    """Decode *raw* with the store codec; return it, its traced heap, and ms."""
    tracemalloc.start()
    start = time.perf_counter()
    payload = msgspec.json.decode(raw, type=_StoredPayload)
    elapsed = (time.perf_counter() - start) * 1000
    heap = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    return payload, heap, elapsed


def concurrent_decode_peak(raw: bytes, workers: int) -> int:
    """Peak traced heap while *workers* threads each hold a decoded copy of *raw*."""
    tracemalloc.start()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        held = list(
            pool.map(lambda r: msgspec.json.decode(r, type=_StoredPayload), [raw] * workers)
        )
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    del held
    return peak


def measure_profile(
    profile: PayloadProfile, rows: int, *, max_record_bytes: int
) -> tuple[ProfileMeasurement, bytes]:
    """Run *profile* through the real reduce, store, read and wire paths."""
    client = _MemoryClient()
    summary, handle = asyncio.run(
        reduce_into_store(
            profile,
            rows,
            _CappedStore(client, max_record_bytes),
            max_spill_rows=_default("result_handle_max_spill_rows"),
            sample_byte_budget=_SAMPLE_BYTE_BUDGET,
        )
    )
    raw = next(iter(client.values.values()), b"")
    stored, heap, decode_ms = _decode_cost(raw) if raw else (None, 0, 0.0)
    # Work is counted over the rows the store kept, so it lines up with
    # ``record_bytes`` when the per-record cap cut the tail.
    work = measure_admission_work(profile.payload(stored.stored_rows if stored else 0))
    page = _largest_page(
        stored.rows if stored else [],
        _default("result_query_max_output_rows"),
        _default("result_query_max_output_bytes"),
    )
    page_payload = {
        "handle_id": str(uuid4()),
        "rows": page,
        "offset": 0,
        "limit": len(page),
        "returned_rows": len(page),
        "total_rows": stored.total_rows if stored else 0,
        "stored_rows": stored.stored_rows if stored else 0,
        "truncated": bool(stored and stored.stored_rows < stored.total_rows),
    }
    completed = OperationResult(
        status="ok",
        op_id=profile.name,
        result=summary,
        duration_ms=1234.5,
        handle=handle,
        audit_id=uuid4(),
        delivery="complete",
    )
    measurement = ProfileMeasurement(
        profile=profile.name,
        captured_rows=rows,
        stored_rows=stored.stored_rows if stored else 0,
        record_bytes=len(raw),
        cache_bytes=valkey_allocation_bytes(len(raw)) if raw else 0,
        nodes=work.nodes,
        decoded_bytes=work.decoded_bytes,
        depth=work.depth,
        page_rows=len(page),
        page_wire_bytes=measure_delivered_wire_bytes(page_payload),
        complete_wire_bytes=measure_delivered_wire_bytes(completed.model_dump(mode="json")),
        decode_heap_bytes=heap,
        decode_ms=round(decode_ms, 1),
    )
    return measurement, raw


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """Measure every profile and derive the recommendations."""
    cap = _default("result_handle_max_record_bytes")
    results = [measure_profile(p, args.rows, max_record_bytes=cap) for p in PROFILES]
    measurements = [m for m, _ in results]
    largest = max((raw for _, raw in results), key=len)
    workers = _default("result_query_executor_workers")
    recs = derive_recommendations(
        measurements,
        full_size_handles=args.full_size_handles,
        headroom=args.headroom,
        max_record_bytes=cap,
    )
    return {
        "measurements": measurements,
        "recommendations": recs,
        "workers": workers,
        "concurrent_peak_bytes": concurrent_decode_peak(largest, workers) if largest else 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure stored result handles for representative vendor payloads and "
            "print recommended values for the result-cache maxmemory, the reducer "
            "work guard, and both wire ceilings."
        )
    )
    parser.add_argument("--rows", type=int, default=10_000, help="rows per payload (default 10000)")
    parser.add_argument(
        "--full-size-handles",
        type=int,
        default=32,
        help="records at the per-record cap the cache must hold at once (default 32)",
    )
    parser.add_argument(
        "--headroom", type=float, default=1.25, help="factor over the largest measured value"
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Prints the results; returns the exit code."""
    args = _parser().parse_args(argv)
    if args.rows <= 0 or args.full_size_handles <= 0 or args.headroom < 1:
        print("--rows and --full-size-handles must be > 0 and --headroom >= 1", file=sys.stderr)
        return 2
    # The store and reducer log every spill; keep stdout for the results.
    saved = structlog.get_config()
    structlog.configure(
        logger_factory=structlog.ReturnLoggerFactory(), cache_logger_on_first_use=False
    )
    try:
        result = run_benchmark(args)
    finally:
        structlog.configure(**saved)
    if args.format == "json":
        out = {
            "measurements": [asdict(m) for m in result["measurements"]],
            "recommendations": [asdict(r) for r in result["recommendations"]],
            "workers": result["workers"],
            "concurrent_peak_bytes": result["concurrent_peak_bytes"],
        }
        print(json.dumps(out, indent=2))
    else:
        print(
            render_markdown(
                result["measurements"],
                result["recommendations"],
                workers=result["workers"],
                concurrent_peak_bytes=result["concurrent_peak_bytes"],
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
