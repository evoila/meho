# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from __future__ import annotations

import random
from functools import lru_cache
from typing import Any

import msgspec

from ..utils.sampling import ReservoirSampler
from .models import (
    ALL_KINDS,
    KIND_ORDER,
    PRIMITIVE_KINDS,
    ArraySummary,
    Summary,
    TreeNode,
    kind_map_get,
)

# Local alias for hot path
_kind_map_get = kind_map_get


def kind_of(v: Any) -> str:
    """Classify a Python value into a JSON kind string."""
    return _kind_map_get(type(v), "unknown")


# --------------------------
# Cached formatting functions
# --------------------------


@lru_cache(maxsize=32)
def fmt_types(types: frozenset) -> str:
    """Cached type formatting."""
    ordered = [t for t in ("str", "int", "float", "bool", "null") if t in types]
    return "|".join(ordered) if ordered else "unknown"


@lru_cache(maxsize=1024)
def fmt_count(min_v: int, max_v: int) -> str:
    return f"[{min_v}]" if min_v == max_v else f"[{min_v}..{max_v}]"


def jsonish_sample(v: Any, max_len: int) -> str:
    """Format a value as JSON-like string, truncating if needed."""
    if type(v) is str and len(v) > max_len:
        v = v[:max_len] + "…"
    return msgspec.json.encode(v).decode("utf-8")


def samples_suffix(
    store: dict[tuple[str, ...], ReservoirSampler],
    path: tuple[str, ...],
    max_sample_len: int,
    samples_k: int,
) -> str:
    """Generate sample values suffix for tree labels."""
    if samples_k <= 0:
        return ""
    sampler = store.get(path)
    vals = sampler.get_exact_k() if sampler else [None] * samples_k
    rendered = ", ".join(jsonish_sample(x, max_sample_len) for x in vals)
    return f" samples=[{rendered}]"


def merge_summary(a: Summary, b: Summary) -> Summary:
    primitives = a.primitives | b.primitives
    truncated = a.truncated or b.truncated
    obj = None
    arr = None

    if a.obj is not None or b.obj is not None:
        obj = {}
        a_obj = a.obj or {}
        b_obj = b.obj or {}
        all_keys = set(a_obj.keys()) | set(b_obj.keys())
        for k in all_keys:
            a_child = a_obj.get(k)
            b_child = b_obj.get(k)
            if a_child and b_child:
                obj[k] = merge_summary(a_child, b_child)
            elif a_child:
                obj[k] = a_child
            else:
                obj[k] = b_child

    if a.arr is not None or b.arr is not None:
        if a.arr is None:
            arr = b.arr
        elif b.arr is None:
            arr = a.arr
        else:
            kind_minmax = {}
            for kind in ALL_KINDS:
                a_mm = a.arr.kind_minmax.get(kind, (0, 0))
                b_mm = b.arr.kind_minmax.get(kind, (0, 0))
                kind_minmax[kind] = (min(a_mm[0], b_mm[0]), max(a_mm[1], b_mm[1]))

            kind_summaries = {}
            for kind in ("object", "array"):
                a_ks = a.arr.kind_summaries.get(kind)
                b_ks = b.arr.kind_summaries.get(kind)
                if a_ks and b_ks:
                    kind_summaries[kind] = merge_summary(a_ks, b_ks)
                elif a_ks:
                    kind_summaries[kind] = a_ks
                elif b_ks:
                    kind_summaries[kind] = b_ks

            arr = ArraySummary(
                len_min=min(a.arr.len_min, b.arr.len_min),
                len_max=max(a.arr.len_max, b.arr.len_max),
                kind_minmax=kind_minmax,
                kind_summaries=kind_summaries,
                truncated=a.arr.truncated or b.arr.truncated,
            )

    return Summary(primitives=primitives, obj=obj, arr=arr, truncated=truncated)


class Analyzer:
    """Core logic for analyzing JSON structure."""

    __slots__ = ("max_depth", "max_keys_per_object", "sample_per_kind", "sort_keys")

    def __init__(
        self,
        max_depth: int = 32,
        sample_per_kind: int = 200,
        sort_keys: bool = True,
        max_keys_per_object: int | None = None,
    ):
        self.max_depth = max_depth
        self.sample_per_kind = sample_per_kind
        self.sort_keys = sort_keys
        self.max_keys_per_object = max_keys_per_object

    def summarize(self, v: Any, depth: int) -> Summary:
        """Summarize a JSON value with local variable caching for speed."""
        # Local variable caching for hot path
        _kind_of = kind_of
        _PRIMITIVE_KINDS = PRIMITIVE_KINDS
        _ALL_KINDS = ALL_KINDS
        _merge_summary = merge_summary

        def _summarize(v: Any, depth: int) -> Summary:
            if depth < 0:
                return Summary(truncated=True)

            k = _kind_of(v)

            if k in _PRIMITIVE_KINDS:
                return Summary(primitives=frozenset((k,)))

            if k == "object":
                if depth == 0:
                    return Summary(obj={}, truncated=True)
                keys = list(v.keys())
                if self.sort_keys:
                    keys.sort(key=str)
                truncated = False
                if self.max_keys_per_object is not None and len(keys) > self.max_keys_per_object:
                    keys = keys[: self.max_keys_per_object]
                    truncated = True
                children = {str(key): _summarize(v[key], depth - 1) for key in keys}
                return Summary(obj=children, truncated=truncated)

            if k == "array":
                arr_len = len(v)
                counts = dict.fromkeys(_ALL_KINDS, 0)
                obj_samples: list[Any] = []
                arr_samples: list[Any] = []
                sample_per_kind = self.sample_per_kind

                for el in v:
                    ek = _kind_of(el)
                    counts[ek] = counts.get(ek, 0) + 1
                    if ek == "object" and len(obj_samples) < sample_per_kind:
                        obj_samples.append(el)
                    elif ek == "array" and len(arr_samples) < sample_per_kind:
                        arr_samples.append(el)

                kind_minmax = {kk: (c, c) for kk, c in counts.items()}
                if depth == 0:
                    return Summary(
                        arr=ArraySummary(
                            len_min=arr_len,
                            len_max=arr_len,
                            kind_minmax=kind_minmax,
                            truncated=True,
                        ),
                        truncated=True,
                    )

                kind_summaries: dict[str, Summary] = {}
                if obj_samples:
                    merged = _summarize(obj_samples[0], depth - 1)
                    for el in obj_samples[1:]:
                        merged = _merge_summary(merged, _summarize(el, depth - 1))
                    kind_summaries["object"] = merged
                if arr_samples:
                    merged = _summarize(arr_samples[0], depth - 1)
                    for el in arr_samples[1:]:
                        merged = _merge_summary(merged, _summarize(el, depth - 1))
                    kind_summaries["array"] = merged

                return Summary(
                    arr=ArraySummary(
                        len_min=arr_len,
                        len_max=arr_len,
                        kind_minmax=kind_minmax,
                        kind_summaries=kind_summaries,
                    )
                )

            return Summary(primitives=frozenset(("unknown",)))

        return _summarize(v, depth)

    def collect_samples(
        self,
        root: Any,
        store: dict[tuple[str, ...], ReservoirSampler],
        k: int,
        rng: random.Random,
    ) -> None:
        """Iterative sample collection using explicit stack with local caching."""
        if k <= 0:
            return

        # Local caching for hot path
        _kind_of = kind_of
        _PRIMITIVE_KINDS = PRIMITIVE_KINDS
        store_get = store.get

        # Stack: (value, path_tuple)
        stack: list[tuple[Any, tuple[str, ...]]] = [(root, ())]

        while stack:
            v, path = stack.pop()
            t = _kind_of(v)

            if t in _PRIMITIVE_KINDS:
                spath = (*path[:-1], f"<items:{t}>") if path and path[-1] == "<items>" else path

                sampler = store_get(spath)
                if sampler is None:
                    sampler = ReservoirSampler(k, rng)
                    store[spath] = sampler
                sampler.add(v)

            elif t == "object":
                # Push children in reverse order so they pop in order
                items = list(v.items())
                for key, val in reversed(items):
                    stack.append((val, (*path, str(key))))

            elif t == "array":
                items_path = (*path, "<items>")
                for el in reversed(v):
                    stack.append((el, items_path))


# --------------------------
# Tree building function (optimized)
# --------------------------


def summary_to_tree(
    name: str,
    s: Summary,
    path: tuple[str, ...],
    sample_store: dict[tuple[str, ...], ReservoirSampler],
    max_sample_len: int,
    samples_k: int,
) -> TreeNode:
    """Build tree node from summary (recursive - tree is usually small)."""
    s_obj = s.obj
    s_arr = s.arr
    has_obj = s_obj is not None
    has_arr = s_arr is not None

    if not has_obj and not has_arr:
        label = f"{name}: {fmt_types(s.primitives)}"
        label += samples_suffix(sample_store, path, max_sample_len, samples_k)
        return TreeNode(label=label, children=[])

    children: list[TreeNode] = []

    # Primitive types at this level
    for p in KIND_ORDER:
        if p in s.primitives:
            label = p + samples_suffix(sample_store, path, max_sample_len, samples_k)
            children.append(TreeNode(label=label, children=[]))

    # Object children
    if s_obj is not None:
        for key in s_obj:
            children.append(
                summary_to_tree(
                    key,
                    s_obj[key],
                    (*path, key),
                    sample_store,
                    max_sample_len,
                    samples_k,
                )
            )

    # Array children
    if s_arr is not None:
        if s_arr.len_max == 0:
            children.append(TreeNode(label="<empty> [0]", children=[]))
        else:
            for kind in KIND_ORDER:
                mm = s_arr.kind_minmax.get(kind, (0, 0))
                if mm[1] == 0:
                    continue

                child_label = f"{kind} {fmt_count(mm[0], mm[1])}"
                if kind in PRIMITIVE_KINDS:
                    child_label += samples_suffix(
                        sample_store,
                        (*path, f"<items:{kind}>"),
                        max_sample_len,
                        samples_k,
                    )

                grandchildren: list[TreeNode] = []
                if kind in s_arr.kind_summaries:
                    elem_sum = s_arr.kind_summaries[kind]
                    if kind == "object" and elem_sum.obj is not None:
                        for k2 in elem_sum.obj:
                            grandchildren.append(
                                summary_to_tree(
                                    k2,
                                    elem_sum.obj[k2],
                                    (*path, "<items>", k2),
                                    sample_store,
                                    max_sample_len,
                                    samples_k,
                                )
                            )
                        if elem_sum.truncated:
                            grandchildren.append(TreeNode(label="…", children=[]))
                    elif kind == "array" and elem_sum.arr is not None:
                        nested = Summary(arr=elem_sum.arr, truncated=elem_sum.truncated)
                        grandchildren.append(
                            summary_to_tree(
                                "<items>",
                                nested,
                                (*path, "<items>"),
                                sample_store,
                                max_sample_len,
                                samples_k,
                            )
                        )
                    elif elem_sum.truncated:
                        grandchildren.append(TreeNode(label="…", children=[]))

                if s_arr.truncated:
                    grandchildren.append(TreeNode(label="…", children=[]))

                children.append(TreeNode(label=child_label, children=grandchildren))

    if s.truncated:
        children.append(TreeNode(label="…", children=[]))

    return TreeNode(label=name, children=children)


# --------------------------
# Renderers (iterative for efficiency)
# --------------------------


def render_tree(root: TreeNode) -> str:
    """Render with └── ├── box-drawing connectors (iterative)."""
    lines: list[str] = [root.label]

    # Stack: (node, prefix, is_last)
    stack: list[tuple[TreeNode, str, bool]] = []

    # Initialize with root's children in reverse
    root_children = root.children
    n = len(root_children)
    for i in range(n - 1, -1, -1):
        stack.append((root_children[i], "", i == n - 1))

    while stack:
        node, prefix, is_last = stack.pop()
        connector = "└── " if is_last else "├── "
        lines.append(prefix + connector + node.label)
        next_prefix = prefix + ("    " if is_last else "│   ")

        children = node.children
        n = len(children)
        for i in range(n - 1, -1, -1):
            stack.append((children[i], next_prefix, i == n - 1))

    return "\n".join(lines)


def render_tabs(root: TreeNode, indent_str: str = "\t") -> str:
    """Render with tab indentation (TSV-like, iterative)."""
    lines: list[str] = [root.label]

    # Stack: (node, level)
    stack: list[tuple[TreeNode, int]] = []
    for ch in reversed(root.children):
        stack.append((ch, 1))

    while stack:
        node, level = stack.pop()
        lines.append(indent_str * level + node.label)
        for ch in reversed(node.children):
            stack.append((ch, level + 1))

    return "\n".join(lines)


# Alias for backward compatibility


def render_bracket(root: TreeNode, indent_str: str = "\t") -> str:
    """Render with curly brace nesting (iterative)."""
    lines: list[str] = []

    # Stack entries: (node, level, is_close)
    stack: list[tuple[TreeNode, int, bool]] = [(root, 0, False)]

    while stack:
        node, level, is_close = stack.pop()
        pad = indent_str * level

        if is_close:
            lines.append(f"{pad}}}")
        elif node.children:
            lines.append(f"{pad}{node.label} {{")
            stack.append((node, level, True))  # Push close brace
            for ch in reversed(node.children):
                stack.append((ch, level + 1, False))
        else:
            lines.append(f"{pad}{node.label}")

    return "\n".join(lines)


# --------------------------
# LLM-friendly schema (compact, TypeScript-like)
# --------------------------


def summary_to_schema(
    s: Summary,
    path: tuple[str, ...] = (),
    sample_store: dict[tuple[str, ...], ReservoirSampler] | None = None,
    max_sample_len: int = 60,
    samples_k: int = 0,
    indent: int = 0,
    inline: bool = False,
    max_depth: int | None = None,
) -> str:
    """
    Convert Summary to compact TypeScript-like schema for LLM consumption.

    Output format is designed to be:
    - Token-efficient (minimal syntax)
    - Clear about types and structure
    - Easy for LLMs to parse and use for query generation

    Args:
        s: Summary to convert
        path: Current path tuple for sample lookup
        sample_store: Optional sample store for including examples
        max_sample_len: Max length for sample strings
        samples_k: Number of samples to include (0 = none)
        indent: Current indentation level
        inline: Whether to use compact inline format
        max_depth: Maximum nesting depth for schema rendering.
                   Beyond this, objects render as ``{...}`` and arrays as
                   ``[...]``.  ``None`` means unlimited (the old default).

    Examples:
        {name: str, age: int}
        [{id: int, name: str, score: float?}]
        [int|str]
        {name: str}  # samples=["Alice", "Bob"]  (with samples)
    """
    # Depth guard: truncate deeply nested structures to save tokens
    if max_depth is not None and indent >= max_depth:
        has_obj = s.obj is not None and bool(s.obj)
        has_arr = s.arr is not None
        prim = [t for t in ("str", "int", "float", "bool", "null") if t in s.primitives]
        abbr: list[str] = []
        if has_obj:
            abbr.append("{...}")
        if has_arr:
            abbr.append("[...]")
        abbr.extend(prim)
        return " | ".join(abbr) if abbr else "unknown"

    pad = "  " * indent
    s_obj = s.obj
    s_arr = s.arr
    primitives = s.primitives

    parts: list[str] = []

    # Helper to get sample suffix
    def get_samples(p: tuple[str, ...], kind: str | None = None) -> str:
        if samples_k <= 0 or sample_store is None:
            return ""
        # For array items, use the kind-specific path
        lookup_path = (*p, f"<items:{kind}>") if kind else p
        return samples_suffix(sample_store, lookup_path, max_sample_len, samples_k)

    # Collect primitive types
    prim_types = [t for t in ("str", "int", "float", "bool", "null") if t in primitives]

    # Handle object
    if s_obj is not None:
        if not s_obj:  # empty object
            parts.append("{}")
        else:
            fields: list[str] = []
            for key, child in s_obj.items():
                child_path = (*path, key)
                child_schema = summary_to_schema(
                    child,
                    child_path,
                    sample_store,
                    max_sample_len,
                    samples_k,
                    indent + 1,
                    inline=True,
                    max_depth=max_depth,
                )
                fields.append(f"{key}: {child_schema}")

            if inline and len(fields) <= 3 and all(len(f) < 30 for f in fields):
                # Compact inline: {a: int, b: str}
                parts.append("{" + ", ".join(fields) + "}")
            else:
                # Multi-line for readability
                inner_pad = "  " * (indent + 1)
                field_lines = [f"{inner_pad}{f}" for f in fields]
                parts.append("{\n" + "\n".join(field_lines) + "\n" + pad + "}")

    # Handle array
    if s_arr is not None:
        if s_arr.len_max == 0:
            parts.append("[]")
        else:
            # Collect element types
            elem_types: list[str] = []

            for kind in KIND_ORDER:
                mm = s_arr.kind_minmax.get(kind, (0, 0))
                if mm[1] == 0:
                    continue

                if kind in PRIMITIVE_KINDS:
                    sample_str = get_samples(path, kind)
                    elem_types.append(f"{kind}{sample_str}" if sample_str else kind)
                elif (kind == "object" and kind in s_arr.kind_summaries) or (
                    kind == "array" and kind in s_arr.kind_summaries
                ):
                    elem_schema = summary_to_schema(
                        s_arr.kind_summaries[kind],
                        (*path, "<items>"),
                        sample_store,
                        max_sample_len,
                        samples_k,
                        indent,
                        inline=True,
                        max_depth=max_depth,
                    )
                    elem_types.append(elem_schema)
                elif kind == "object":
                    elem_types.append("{...}")
                elif kind == "array":
                    elem_types.append("[...]")

            if len(elem_types) == 1:
                parts.append(f"[{elem_types[0]}]")
            elif elem_types:
                parts.append(f"[{' | '.join(elem_types)}]")
            else:
                parts.append("[]")

    # Add primitive types with samples
    if prim_types:
        if len(prim_types) == 1 and not s_obj and not s_arr:
            # Single primitive type at leaf - add samples
            sample_str = get_samples(path)
            if sample_str:
                parts.append(f"{prim_types[0]}{sample_str}")
            else:
                parts.append(prim_types[0])
        else:
            parts.extend(prim_types)

    # Combine all parts
    if not parts:
        return "unknown"

    if len(parts) == 1:
        return parts[0]

    # Multiple types - use union
    # Simplify: if we have "null" and one other type, use ? suffix
    if len(parts) == 2 and "null" in parts:
        other = next(p for p in parts if p != "null")
        # Only use ? for simple types, not complex objects/arrays
        if other in ("str", "int", "float", "bool"):
            return f"{other}?"

    return " | ".join(parts)


def render_schema(
    root_summary: Summary,
    sample_store: dict[tuple[str, ...], ReservoirSampler] | None = None,
    max_sample_len: int = 60,
    samples_k: int = 0,
    max_depth: int | None = None,
) -> str:
    """
    Render a compact schema from the root Summary.

    This is the main entry point for LLM-friendly schema generation.
    Returns a TypeScript-like schema string.

    Args:
        root_summary: The Summary to render
        sample_store: Optional sample store for including examples
        max_sample_len: Max length for sample strings
        samples_k: Number of samples to include (0 = none)
        max_depth: Maximum nesting depth to render.  Structures deeper
                   than this are collapsed to ``{...}`` / ``[...]``.
                   ``None`` means unlimited.
    """
    return summary_to_schema(
        root_summary,
        path=(),
        sample_store=sample_store,
        max_sample_len=max_sample_len,
        samples_k=samples_k,
        indent=0,
        inline=False,
        max_depth=max_depth,
    )
