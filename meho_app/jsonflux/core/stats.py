# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from __future__ import annotations

import contextlib
import time
from typing import Any

import msgspec

from .models import kind_map_get

# Local alias for hot path
_kind_map_get = kind_map_get


def estimate_json_size(v: Any) -> int:
    """Estimate JSON-serialized size in bytes (fast approximation)."""
    t = type(v)
    if v is None:
        return 4  # "null"
    elif t is bool:
        return 5 if v else 4  # "true" or "false"
    elif t is int or t is float:
        return len(str(v))
    elif isinstance(v, str):
        # Account for quotes and potential escaping (rough estimate)
        return len(v) + 2 + v.count('"') + v.count("\\")
    elif t is list:
        return 2  # Just brackets, items counted separately
    elif t is dict:
        return 2  # Just braces, keys/values counted separately
    return 0


def fmt_size(n: int) -> str:
    """Format bytes as human-readable string with appropriate unit."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.2f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    else:
        return f"{n / (1024 * 1024 * 1024):.2f} GB"


# Alias for backward compatibility
fmt_bytes = fmt_size


class FieldStats:
    __slots__ = (
        "arr_count",
        "arr_empty_count",
        "arr_max_len",
        "arr_min_len",
        "arr_total_len",
        "bool_false_count",
        "bool_true_count",
        "null_count",
        "num_count",
        "num_max",
        "num_min",
        "num_sum",
        "obj_count",
        "obj_empty_count",
        "obj_key_counts",
        "path",
        "size_max",
        "size_min",
        "str_count",
        "str_empty_count",
        "str_max_len",
        "str_min_len",
        "str_total_len",
        "total_seen",
        "total_size",
        "type_counts",
        "unique_limit",
        "unique_values",
    )

    def __init__(self, path: str, unique_limit: int = 100) -> None:
        self.path = path
        self.total_seen = 0
        self.null_count = 0
        self.type_counts: dict[str, int] = {}
        self.total_size = 0
        self.size_min: int | None = None
        self.size_max: int | None = None
        self.num_min: float | None = None
        self.num_max: float | None = None
        self.num_sum = 0.0
        self.num_count = 0
        self.str_min_len: int | None = None
        self.str_max_len: int | None = None
        self.str_total_len = 0
        self.str_count = 0
        self.str_empty_count = 0
        self.arr_min_len: int | None = None
        self.arr_max_len: int | None = None
        self.arr_total_len = 0
        self.arr_count = 0
        self.arr_empty_count = 0
        self.obj_key_counts: dict[str, int] = {}
        self.obj_count = 0
        self.obj_empty_count = 0
        self.bool_true_count = 0
        self.bool_false_count = 0
        self.unique_values: set[Any] = set()
        self.unique_limit = unique_limit

    def add(self, value: Any) -> None:  # NOSONAR (cognitive complexity)
        self.total_seen += 1
        t = type(value)
        size = estimate_json_size(value)
        self.total_size += size
        if self.size_min is None or size < self.size_min:
            self.size_min = size
        if self.size_max is None or size > self.size_max:
            self.size_max = size

        if value is None:
            self.null_count += 1
            self.type_counts["null"] = self.type_counts.get("null", 0) + 1
            return

        type_name = _kind_map_get(t, "unknown")
        self.type_counts[type_name] = self.type_counts.get(type_name, 0) + 1

        if t is bool:
            if value:
                self.bool_true_count += 1
            else:
                self.bool_false_count += 1
            self._track_unique(value)
        elif t in (int, float):
            v = float(value)
            self.num_count += 1
            self.num_sum += v
            if self.num_min is None or v < self.num_min:
                self.num_min = v
            if self.num_max is None or v > self.num_max:
                self.num_max = v
            self._track_unique(value)
        elif t is str:
            length = len(value)
            self.str_count += 1
            self.str_total_len += length
            if length == 0:
                self.str_empty_count += 1
            if self.str_min_len is None or length < self.str_min_len:
                self.str_min_len = length
            if self.str_max_len is None or length > self.str_max_len:
                self.str_max_len = length
            if length <= 100:
                self._track_unique(value)
        elif t is list:
            length = len(value)
            self.arr_count += 1
            self.arr_total_len += length
            if length == 0:
                self.arr_empty_count += 1
            if self.arr_min_len is None or length < self.arr_min_len:
                self.arr_min_len = length
            if self.arr_max_len is None or length > self.arr_max_len:
                self.arr_max_len = length
        elif t is dict:
            self.obj_count += 1
            if len(value) == 0:
                self.obj_empty_count += 1
            for key in value:
                k = str(key)
                self.obj_key_counts[k] = self.obj_key_counts.get(k, 0) + 1

    def _track_unique(self, value: Any) -> None:
        if len(self.unique_values) < self.unique_limit:
            with contextlib.suppress(TypeError):
                self.unique_values.add(value)

    def format_report(self) -> str:
        lines = [
            f"📍 {self.path}",
            f"   Count: {self.total_seen:,} | Size: {fmt_size(self.total_size)}",
        ]
        if self.null_count > 0:
            lines.append(
                f"   Null: {self.null_count} ({self.null_count / self.total_seen * 100:.1f}%)"
            )

        type_parts = []
        for t, c in sorted(self.type_counts.items(), key=lambda x: -x[1]):
            type_parts.append(f"{t}:{c}({c / self.total_seen * 100:.1f}%)")
        lines.append(f"   Types: {', '.join(type_parts)}")

        if self.num_count > 0:
            lines.append(
                f"   Numeric: min={self.num_min}, max={self.num_max}, avg={self.num_sum / self.num_count:.2f}"
            )
        if self.str_count > 0:
            lines.append(
                f"   String: len={self.str_min_len}..{self.str_max_len}, avg={self.str_total_len / self.str_count:.1f} chars"
            )
        if self.arr_count > 0:
            lines.append(
                f"   Array: len={self.arr_min_len}..{self.arr_max_len}, avg={self.arr_total_len / self.arr_count:.1f} items"
            )
        if self.obj_key_counts:
            lines.append(f"   Keys: {len(self.obj_key_counts)} unique keys")

        return "\n".join(lines)


class StatsResult(msgspec.Struct, gc=False):
    field_stats: dict[str, FieldStats]
    total_values: int
    total_objects: int
    total_arrays: int
    total_primitives: int
    total_size_bytes: int
    max_depth: int
    collection_time: float = 0.0

    def __str__(self) -> str:
        lines = [
            " " + "=" * 68,
            " JSON STATISTICS",
            " " + "=" * 68,
            f" Total values:     {self.total_values:,}",
            f"   Objects:        {self.total_objects:,}",
            f"   Arrays:         {self.total_arrays:,}",
            f"   Primitives:     {self.total_primitives:,}",
            f" Estimated size:   {fmt_size(self.total_size_bytes)}",
            f" Max depth:        {self.max_depth}",
            f" Unique paths:     {len(self.field_stats):,}",
            f" Collection time:  {self.collection_time:.3f}s",
            " " + "-" * 68,
            "",
        ]
        # Show top 20 paths by size
        sorted_fields = sorted(self.field_stats.values(), key=lambda x: -x.total_size)[:20]
        for fs in sorted_fields:
            lines.append(fs.format_report())
            lines.append("")

        if len(self.field_stats) > 20:
            lines.append(f"... and {len(self.field_stats) - 20} more paths")

        return "\n".join(lines)


# --------------------------
# Stats formatting functions
# --------------------------


def format_stats_compact(stats: StatsResult) -> str:  # NOSONAR (cognitive complexity)
    """Format statistics as a compact summary (no per-path breakdown)."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("JSON STATISTICS (COMPACT)")
    lines.append("=" * 60)

    # Value counts
    total = stats.total_values
    lines.append(f"Total values:       {total:,}")
    lines.append(
        f"  Objects:          {stats.total_objects:,} ({stats.total_objects / total * 100:.1f}%)"
    )
    lines.append(
        f"  Arrays:           {stats.total_arrays:,} ({stats.total_arrays / total * 100:.1f}%)"
    )
    lines.append(
        f"  Primitives:       {stats.total_primitives:,} ({stats.total_primitives / total * 100:.1f}%)"
    )
    lines.append("")

    # Aggregate type distribution across all paths
    type_totals: dict[str, int] = {}
    null_total = 0
    str_total_len = 0
    str_count = 0
    num_min: float | None = None
    num_max: float | None = None
    num_sum = 0.0
    num_count = 0
    bool_true = 0
    bool_false = 0
    empty_arrays = 0
    empty_objects = 0
    empty_strings = 0

    for fs in stats.field_stats.values():
        for t, c in fs.type_counts.items():
            type_totals[t] = type_totals.get(t, 0) + c
        null_total += fs.null_count
        str_total_len += fs.str_total_len
        str_count += fs.str_count
        empty_strings += fs.str_empty_count
        if fs.num_count > 0:
            num_count += fs.num_count
            num_sum += fs.num_sum
            if num_min is None or (fs.num_min is not None and fs.num_min < num_min):
                num_min = fs.num_min
            if num_max is None or (fs.num_max is not None and fs.num_max > num_max):
                num_max = fs.num_max
        bool_true += fs.bool_true_count
        bool_false += fs.bool_false_count
        empty_arrays += fs.arr_empty_count
        empty_objects += fs.obj_empty_count

    # Type distribution
    lines.append("TYPE DISTRIBUTION:")
    type_total = sum(type_totals.values())
    for t in ["object", "array", "str", "int", "float", "bool", "null"]:
        c = type_totals.get(t, 0)
        if c > 0:
            pct = c / type_total * 100
            lines.append(f"  {t:12} {c:>12,}  ({pct:5.1f}%)")
    lines.append("")

    # Size info
    lines.append("SIZE:")
    lines.append(f"  Estimated total:  {fmt_size(stats.total_size_bytes)}")
    lines.append(f"  Avg per value:    {fmt_size(stats.total_size_bytes // max(1, total))}")
    lines.append("")

    # Depth and paths
    lines.append("STRUCTURE:")
    lines.append(f"  Max depth:        {stats.max_depth}")
    lines.append(f"  Unique paths:     {len(stats.field_stats):,}")
    lines.append("")

    # Null rate
    if null_total > 0:
        lines.append("NULL ANALYSIS:")
        lines.append(f"  Total nulls:      {null_total:,} ({null_total / total * 100:.2f}%)")
        lines.append("")

    # String stats
    if str_count > 0:
        lines.append("STRING STATS:")
        lines.append(f"  Total strings:    {str_count:,}")
        lines.append(f"  Avg length:       {str_total_len / str_count:.1f} chars")
        if empty_strings > 0:
            lines.append(
                f"  Empty strings:    {empty_strings:,} ({empty_strings / str_count * 100:.1f}%)"
            )
        lines.append("")

    # Numeric stats
    if num_count > 0:
        lines.append("NUMERIC STATS:")
        lines.append(f"  Total numbers:    {num_count:,}")
        lines.append(f"  Min:              {num_min}")
        lines.append(f"  Max:              {num_max}")
        lines.append(f"  Avg:              {num_sum / num_count:.4f}")
        lines.append("")

    # Boolean stats
    if bool_true + bool_false > 0:
        total_bool = bool_true + bool_false
        lines.append("BOOLEAN STATS:")
        lines.append(f"  Total booleans:   {total_bool:,}")
        lines.append(f"  True:             {bool_true:,} ({bool_true / total_bool * 100:.1f}%)")
        lines.append(f"  False:            {bool_false:,} ({bool_false / total_bool * 100:.1f}%)")
        lines.append("")

    # Empty collections
    if empty_arrays > 0 or empty_objects > 0:
        lines.append("EMPTY COLLECTIONS:")
        if empty_arrays > 0:
            lines.append(f"  Empty arrays:     {empty_arrays:,}")
        if empty_objects > 0:
            lines.append(f"  Empty objects:    {empty_objects:,}")
        lines.append("")

    lines.append(f"Collection time:    {stats.collection_time:.3f}s")
    lines.append("=" * 60)

    return "\n".join(lines)


def format_stats(stats: StatsResult, top_n: int = 50) -> str:  # NOSONAR (cognitive complexity)
    """Format statistics as a readable string with per-path breakdown."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("JSON STATISTICS")
    lines.append("=" * 70)
    lines.append(f"Total values:     {stats.total_values:,}")
    lines.append(f"  Objects:        {stats.total_objects:,}")
    lines.append(f"  Arrays:         {stats.total_arrays:,}")
    lines.append(f"  Primitives:     {stats.total_primitives:,}")
    lines.append(f"Estimated size:   {fmt_size(stats.total_size_bytes)}")
    lines.append(f"Max depth:        {stats.max_depth}")
    lines.append(f"Unique paths:     {len(stats.field_stats):,}")
    lines.append(f"Collection time:  {stats.collection_time:.3f}s")
    lines.append("-" * 70)
    lines.append("")

    # Sort by total size (most data first)
    sorted_stats = sorted(stats.field_stats.values(), key=lambda x: -x.total_size)[:top_n]

    for fs in sorted_stats:
        lines.append(f"📍 {fs.path}")
        lines.append(f"   Count: {fs.total_seen:,}  |  Size: {fmt_size(fs.total_size)}")

        # Size breakdown
        if fs.total_seen > 0:
            avg_size = fs.total_size / fs.total_seen
            lines.append(
                f"   Size/value: min={fmt_size(fs.size_min or 0)}, "
                f"max={fmt_size(fs.size_max or 0)}, avg={fmt_size(int(avg_size))}"
            )

        # Type distribution
        type_parts = []
        for t, c in sorted(fs.type_counts.items(), key=lambda x: -x[1]):
            pct = c / fs.total_seen * 100
            type_parts.append(f"{t}:{c}({pct:.1f}%)")
        lines.append(f"   Types: {', '.join(type_parts)}")

        # Null rate
        if fs.null_count > 0:
            null_pct = fs.null_count / fs.total_seen * 100
            lines.append(f"   Null: {fs.null_count:,} ({null_pct:.1f}%)")

        # Numeric stats
        if fs.num_count > 0:
            avg = fs.num_sum / fs.num_count
            lines.append(f"   Numeric: min={fs.num_min}, max={fs.num_max}, avg={avg:.2f}")

        # String stats
        if fs.str_count > 0:
            avg_len = fs.str_total_len / fs.str_count
            lines.append(f"   String: len={fs.str_min_len}..{fs.str_max_len}, avg={avg_len:.1f}")
            if fs.str_empty_count > 0:
                lines.append(f"   String empty: {fs.str_empty_count:,}")

        # Array stats
        if fs.arr_count > 0:
            avg_len = fs.arr_total_len / fs.arr_count
            lines.append(f"   Array: len={fs.arr_min_len}..{fs.arr_max_len}, avg={avg_len:.1f}")
            if fs.arr_empty_count > 0:
                lines.append(f"   Array empty: {fs.arr_empty_count:,}")

        # Boolean stats
        if fs.bool_true_count + fs.bool_false_count > 0:
            total_bool = fs.bool_true_count + fs.bool_false_count
            true_pct = fs.bool_true_count / total_bool * 100
            lines.append(
                f"   Boolean: true={fs.bool_true_count} ({true_pct:.1f}%), false={fs.bool_false_count}"
            )

        # Unique values
        if fs.unique_values:
            n = len(fs.unique_values)
            if n <= 10:
                vals = sorted(
                    [v for v in fs.unique_values if v is not None],
                    key=lambda x: str(x),
                )[:10]
                lines.append(f"   Unique({n}): {vals}")
            else:
                note = "+" if n >= fs.unique_limit else ""
                lines.append(f"   Unique: {n}{note} distinct values")

        # Object key presence
        if fs.obj_count > 0 and fs.obj_key_counts:
            lines.append(f"   Object keys ({len(fs.obj_key_counts)}):")
            for k, c in sorted(fs.obj_key_counts.items(), key=lambda x: -x[1])[:10]:
                pct = c / fs.obj_count * 100
                lines.append(f"      .{k}: {c} ({pct:.1f}%)")

        lines.append("")

    if len(stats.field_stats) > top_n:
        lines.append(f"... and {len(stats.field_stats) - top_n} more paths")

    return "\n".join(lines)


def collect_stats(  # NOSONAR (cognitive complexity)
    root: Any,
    *,
    max_unique: int = 100,
) -> StatsResult:
    """
    Collect comprehensive statistics about a JSON document.
    Iterative traversal for speed.
    """
    t0 = time.perf_counter()

    field_stats: dict[str, FieldStats] = {}
    total_values = 0
    total_objects = 0
    total_arrays = 0
    total_primitives = 0
    total_size_bytes = 0
    max_depth = 0

    # Stack: (value, path_str, depth)
    stack: list[tuple[Any, str, int]] = [(root, "$", 0)]

    while stack:
        v, path, depth = stack.pop()
        total_values += 1
        if depth > max_depth:
            max_depth = depth

        # Track size
        size = estimate_json_size(v)
        total_size_bytes += size

        # Get or create field stats
        fs = field_stats.get(path)
        if fs is None:
            fs = FieldStats(path, unique_limit=max_unique)
            field_stats[path] = fs

        fs.add(v)
        t = type(v)

        if t is dict:
            total_objects += 1
            # Add size for keys and separators
            for key, val in v.items():
                total_size_bytes += len(str(key)) + 3  # "key":
                child_path = f"{path}.{key}"
                stack.append((val, child_path, depth + 1))
            if v:
                total_size_bytes += len(v) - 1  # commas between items

        elif t is list:
            total_arrays += 1
            item_path = f"{path}[]"
            for el in v:
                stack.append((el, item_path, depth + 1))
            if v:
                total_size_bytes += len(v) - 1  # commas between items

        else:
            total_primitives += 1

    collection_time = time.perf_counter() - t0

    return StatsResult(
        field_stats=field_stats,
        total_values=total_values,
        total_objects=total_objects,
        total_arrays=total_arrays,
        total_primitives=total_primitives,
        total_size_bytes=total_size_bytes,
        max_depth=max_depth,
        collection_time=collection_time,
    )
