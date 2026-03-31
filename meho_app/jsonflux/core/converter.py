# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
from typing import Any

import pyarrow as pa

from .models import Summary


def summary_to_arrow_type(summary: Summary) -> pa.DataType:
    """
    Convert a JsonFlux Summary to a PyArrow DataType.

    Strategies for type conflict resolution:
    - Mixed primitives (int + float) -> float
    - Mixed primitives (int/float + str) -> string
    - Mixed complex (object + array) -> string (JSON dump)
    - Unknown/Null only -> string (safe default)
    """
    prims = summary.primitives

    # 1. Complex types (Object/Array) need special handling
    has_obj = summary.obj is not None
    has_arr = summary.arr is not None

    if has_obj and has_arr:
        # Conflict: field is both object and array in different rows
        return pa.string()

    if has_obj:
        # If it also has primitives (e.g. obj | null is fine, but obj | int -> string)
        non_null_prims = [p for p in prims if p != "null"]
        if non_null_prims:
            return pa.string()

        # DuckDB does not support zero-field structs.
        # If the object is empty across all samples, we use string as a fallback.
        if not summary.obj:
            return pa.string()

        # Recursive struct construction
        fields = []
        # Sort keys for deterministic schema
        for key in sorted(summary.obj.keys()):
            child_type = summary_to_arrow_type(summary.obj[key])
            fields.append(pa.field(key, child_type, nullable=True))
        return pa.struct(fields)

    if has_arr:
        non_null_prims = [p for p in prims if p != "null"]
        if non_null_prims:
            return pa.string()

        # Determine array item type
        # We need to merge all "kind_summaries" in the array summary to find a unified type
        # For now, let's look at what kinds are present in the array

        # ArraySummary has: kind_summaries for 'object' and 'array'
        # and kind_minmax for primitives.

        # We need to synthesize a single Summary for the items
        # This is strictly speaking a bit lossy if we don't have a pre-merged item summary
        # But we can reconstruct it from the ArraySummary parts.

        # Check primitives in array
        arr_prims = set()
        for kind in ("str", "int", "float", "bool", "null"):
            if summary.arr.kind_minmax.get(kind, (0, 0))[1] > 0:
                arr_prims.add(kind)

        # Check complex in array
        obj_summary = summary.arr.kind_summaries.get("object")
        arr_summary = summary.arr.kind_summaries.get("array")

        # If mixed types in array -> string
        # e.g. [1, "a"] -> list<string>
        # e.g. [{"a": 1}, 2] -> list<string>

        has_complex_items = (obj_summary is not None) or (arr_summary is not None)
        has_prim_items = len(arr_prims - {"null"}) > 0

        if has_complex_items and has_prim_items:
            # Mix of objects and primitives in list -> list<string> (or stringify whole list?)
            # Usually list<string> is better so we can at least unnest
            return pa.list_(pa.string())

        if has_complex_items:
            if obj_summary and arr_summary:
                return pa.list_(pa.string())
            if obj_summary:
                return pa.list_(summary_to_arrow_type(obj_summary))
            if arr_summary:
                # List of lists
                # We need to wrap the inner array summary back into a full Summary to recurse
                inner_s = Summary(arr=arr_summary.arr, truncated=arr_summary.truncated)
                return pa.list_(summary_to_arrow_type(inner_s))

        # Only primitives
        if not arr_prims or arr_prims == {"null"}:
            return pa.list_(pa.string())  # Empty/Null list

        if "str" in arr_prims:
            return pa.list_(pa.string())
        if "float" in arr_prims:
            return pa.list_(pa.float64())
        if "int" in arr_prims:
            return pa.list_(pa.int64())
        if "bool" in arr_prims:
            return pa.list_(pa.bool_())

        return pa.list_(pa.string())

    # 2. Primitives only
    non_null_prims = [p for p in prims if p != "null"]

    if not non_null_prims:
        return pa.string()  # Default for "null" only or empty

    if "str" in non_null_prims:
        return pa.string()

    if "float" in non_null_prims:
        return pa.float64()

    if "int" in non_null_prims:
        # Check range if we tracked it? For now assume int64
        return pa.int64()

    if "bool" in non_null_prims:
        return pa.bool_()

    return pa.string()


def summary_to_schema(summary: Summary) -> pa.Schema:
    """Convert root Summary to PyArrow Schema."""
    if summary.obj is None:
        # Root must be object to be a "Table" (or we treat array as 1 col?)
        # For direct JSON mapping, usually root is struct.
        # If root is array, we might treat it as a table where each item is a row.
        raise ValueError("Root summary must be an object to convert to Schema")

    fields = []
    for key in sorted(summary.obj.keys()):
        field_type = summary_to_arrow_type(summary.obj[key])
        fields.append(pa.field(key, field_type, nullable=True))

    return pa.schema(fields)


def normalize_data(data: Any, schema: pa.DataType | pa.Schema) -> Any:
    """
    Recursively normalize data to match the Arrow schema.

    Handles:
    - safely casting primitives to string (if schema expects string)
    - casting int to float
    - filtering unknown fields (optional, but good for struct strictness)
    - handling nulls
    """
    if data is None:
        return None

    # Handle Schema vs DataType
    if isinstance(schema, pa.Schema):
        if not isinstance(data, dict):
            # If root is a list of records, we might need to handle that outside
            # But usually we normalize a single record or list of records
            return data

        out = {}
        for field in schema:
            val = data.get(field.name)
            out[field.name] = normalize_data(val, field.type)
        return out

    # Handle DataTypes
    if isinstance(schema, pa.StructType):
        if not isinstance(data, dict):
            # Fallback if structure mismatches (e.g. schema says struct, data has "foo")
            return None
        out = {}
        for field in schema:
            val = data.get(field.name)
            out[field.name] = normalize_data(val, field.type)
        return out

    if isinstance(schema, pa.ListType):
        if not isinstance(data, list):
            return []  # expected list, got something else

        item_type = schema.value_type
        return [normalize_data(x, item_type) for x in data]

    if pa.types.is_string(schema):
        if isinstance(data, (dict, list)):
            import json

            return json.dumps(data)
        return str(data)

    if pa.types.is_float64(schema):
        try:
            return float(data)
        except (ValueError, TypeError):
            return None

    if pa.types.is_int64(schema):
        try:
            return int(data)
        except (ValueError, TypeError):
            # Determine strategy: return None or 0? None is safer
            return None

    if pa.types.is_boolean(schema):
        return bool(data)

    return data
