# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from __future__ import annotations

from typing import Any

import msgspec

# --------------------------
# Shared constants (canonical definitions)
# --------------------------

ALL_KINDS: tuple[str, ...] = ("object", "array", "str", "int", "float", "bool", "null")
KIND_ORDER: tuple[str, ...] = ALL_KINDS
PRIMITIVE_KINDS: frozenset = frozenset(("str", "int", "float", "bool", "null"))

KIND_MAP: dict[type, str] = {
    type(None): "null",
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    dict: "object",
    list: "array",
}
kind_map_get = KIND_MAP.get  # Cached lookup for hot paths


# --------------------------
# Data models
# --------------------------


class ArraySummary(msgspec.Struct, gc=False):
    len_min: int
    len_max: int
    kind_minmax: dict[str, tuple[int, int]] = {}
    kind_summaries: dict[str, Summary] = {}
    truncated: bool = False


class Summary(msgspec.Struct, gc=False):
    primitives: frozenset = frozenset()
    obj: dict[str, Summary] | None = None
    arr: ArraySummary | None = None
    truncated: bool = False


class TreeNode(msgspec.Struct, gc=False):
    label: str
    children: list[TreeNode] = []


class ProfileResult(msgspec.Struct, gc=False):
    summary: Summary
    sample_store: dict[tuple[str, ...], Any]  # Using Any for ReservoirSampler to avoid circularity
    source: str
    parse_time: float = 0.0
    analyze_time: float = 0.0
    sample_time: float = 0.0
