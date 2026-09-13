#!/usr/bin/env python3
"""Compare two pinned OpenAPI/Swagger API catalogs without network access.

The output is an evidence artifact, not a compatibility verdict.  It expands
local ``$ref`` values before hashing request/response and parameter shapes,
so a changed nested definition is visible on every affected operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
PRESENTATION_KEYS = frozenset({"description", "summary", "externalDocs", "example", "examples"})
PROPERTY_CONTAINER_KEYS = frozenset({"properties", "patternProperties", "headers"})
MAX_REF_DEPTH = 2048


def load_document(path: Path) -> dict[str, Any]:
    """Load a JSON or YAML OpenAPI document into a mapping."""
    text = path.read_text(encoding="utf-8")
    loaded = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not an API-document mapping")
    return loaded


def _local_ref(document: Mapping[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        return {"$ref": reference}
    current: Any = document
    for part in reference[2:].split("/"):
        if not isinstance(current, Mapping):
            raise ValueError(f"local reference does not resolve: {reference}")
        current = current[part.replace("~1", "/").replace("~0", "~")]
    return current


def dereference(document: Mapping[str, Any], value: Any, seen: tuple[str, ...] = ()) -> Any:
    """Resolve local refs recursively, retaining a stable marker for cycles."""
    if len(seen) > MAX_REF_DEPTH:
        raise ValueError(f"local reference chain exceeds {MAX_REF_DEPTH} entries")
    if isinstance(value, list):
        return [dereference(document, item, seen) for item in value]
    if not isinstance(value, Mapping):
        return value
    reference = value.get("$ref")
    if isinstance(reference, str):
        if reference in seen:
            return {"$ref_cycle": reference}
        target = _local_ref(document, reference)
        if isinstance(target, Mapping):
            merged = dict(target)
            merged.update({key: item for key, item in value.items() if key != "$ref"})
            return dereference(document, merged, (*seen, reference))
        return dereference(document, target, (*seen, reference))
    return {str(key): dereference(document, item, seen) for key, item in value.items()}


def _without_presentation(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, list):
        return [_without_presentation(item, parent_key=parent_key) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _without_presentation(item, parent_key=key)
            for key, item in value.items()
            if key not in PRESENTATION_KEYS or parent_key in PROPERTY_CONTAINER_KEYS
        }
    return value


def _presentation(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            item_path = f"{path}/{key}"
            if key in {"description", "summary"} and isinstance(item, str):
                yield item_path, item
            yield from _presentation(item, item_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _presentation(item, f"{path}/{index}")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _base_paths(document: Mapping[str, Any]) -> list[str]:
    if "swagger" in document:
        return [str(document.get("basePath", ""))]
    servers = document.get("servers", [])
    if not isinstance(servers, list):
        return []
    return [str(server.get("url", "")) for server in servers if isinstance(server, Mapping)]


def _request_shape(document: Mapping[str, Any], operation: Mapping[str, Any]) -> Any:
    if "openapi" in document:
        return operation.get("requestBody")
    return [
        parameter for parameter in operation.get("parameters", []) if parameter.get("in") == "body"
    ]


def _effective_parameters(
    document: Mapping[str, Any], path_item: Mapping[str, Any], operation: Mapping[str, Any]
) -> list[Any]:
    combined = [*path_item.get("parameters", []), *operation.get("parameters", [])]
    result: dict[tuple[str, str], Any] = {}
    for raw_parameter in combined:
        parameter = dereference(document, raw_parameter)
        if isinstance(parameter, Mapping):
            result[(str(parameter.get("name", "")), str(parameter.get("in", "")))] = parameter
    return list(result.values())


def _security_contract(document: Mapping[str, Any], operation: Mapping[str, Any]) -> dict[str, Any]:
    requirements = operation.get("security", document.get("security", []))
    schemes = document.get("components", {}).get("securitySchemes", {})
    if not schemes:
        schemes = document.get("securityDefinitions", {})
    names = (
        {name for requirement in requirements for name in requirement} if requirements else set()
    )
    return {"requirements": requirements, "schemes": {name: schemes.get(name) for name in names}}


def _external_refs(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        found = (
            {value["$ref"]}
            if isinstance(value.get("$ref"), str) and not value["$ref"].startswith("#/")
            else set()
        )
        return found | set().union(*(_external_refs(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_external_refs(item) for item in value)) if value else set()
    return set()


def _operation_record(
    document: Mapping[str, Any], path_item: Mapping[str, Any], operation: Mapping[str, Any]
) -> dict[str, str]:
    inherited = _effective_parameters(document, path_item, operation)
    feature_state = {
        "deprecated": bool(operation.get("deprecated", False)),
        "extensions": {key: value for key, value in operation.items() if str(key).startswith("x-")},
    }
    contract = {
        "parameters": inherited,
        "request": _request_shape(document, operation),
        "responses": operation.get("responses", {}),
        "security": _security_contract(document, operation),
        "servers": operation.get("servers", document.get("servers", [])),
        "media_types": {
            "consumes": operation.get("consumes", document.get("consumes", [])),
            "produces": operation.get("produces", document.get("produces", [])),
        },
        "feature_state": feature_state,
    }
    expanded = dereference(document, contract)
    expanded_operation = dereference(document, operation)
    return {
        "contract": _digest(_without_presentation(expanded)),
        "parameters": _digest(_without_presentation(expanded["parameters"])),
        "request": _digest(_without_presentation(expanded["request"])),
        "responses": _digest(_without_presentation(expanded["responses"])),
        "security": _digest(_without_presentation(expanded["security"])),
        "servers": _digest(_without_presentation(expanded["servers"])),
        "media_types": _digest(_without_presentation(expanded["media_types"])),
        "feature_state": _digest(_without_presentation(expanded["feature_state"])),
        "presentation": _digest(sorted(_presentation(expanded_operation))),
    }


def inventory(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable, operation-level catalog inventory for one document."""
    operations: dict[str, dict[str, str]] = {}
    paths = document.get("paths", {})
    if not isinstance(paths, Mapping):
        raise ValueError("API document paths must be a mapping")
    for path, path_item in paths.items():
        if not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if str(method).lower() in HTTP_METHODS and isinstance(operation, Mapping):
                operation_id = f"{str(method).upper()}:{path}"
                operations[operation_id] = _operation_record(document, path_item, operation)
    schemas = document.get("components", {}).get("schemas", document.get("definitions", {}))
    return {
        "base_paths": _base_paths(document),
        "operations": operations,
        "schemas": _digest(_without_presentation(dereference(document, schemas))),
        "external_refs": sorted(_external_refs(document)),
    }


def compare_catalogs(
    before: Mapping[str, Any], after: Mapping[str, Any], *, same_lineage: bool
) -> dict[str, Any]:
    """Classify route, contract, and description-only differences."""
    previous = inventory(before)
    current = inventory(after)
    unresolved = previous["external_refs"] or current["external_refs"]
    if unresolved:
        return {
            "comparison_status": "external-reference-unresolved",
            "external_refs": sorted(set(previous["external_refs"]) | set(current["external_refs"])),
        }
    if not same_lineage:
        return {
            "comparison_status": "artifact-lineage-unknown",
            "before_operations": len(previous["operations"]),
            "after_operations": len(current["operations"]),
            "base_paths_before": previous["base_paths"],
            "base_paths_after": current["base_paths"],
            "added": [],
            "removed": [],
            "changed": [],
            "description_only": [],
        }
    previous_ids = set(previous["operations"])
    current_ids = set(current["operations"])
    changed: list[dict[str, Any]] = []
    description_only: list[str] = []
    for operation_id in sorted(previous_ids & current_ids):
        left = previous["operations"][operation_id]
        right = current["operations"][operation_id]
        contract_fields = (
            "parameters",
            "request",
            "responses",
            "security",
            "servers",
            "media_types",
            "feature_state",
        )
        changed_fields = [key for key in contract_fields if left[key] != right[key]]
        if changed_fields:
            changed.append({"operation_id": operation_id, "fields": changed_fields})
        elif left["presentation"] != right["presentation"]:
            description_only.append(operation_id)
    return {
        "comparison_status": "same-artifact-lineage",
        "before_operations": len(previous_ids),
        "after_operations": len(current_ids),
        "base_paths_before": previous["base_paths"],
        "base_paths_after": current["base_paths"],
        "added": sorted(current_ids - previous_ids),
        "removed": sorted(previous_ids - current_ids),
        "changed": changed,
        "description_only": description_only,
        "named_schemas_changed": previous["schemas"] != current["schemas"],
    }


def main() -> int:
    sys.setrecursionlimit(MAX_REF_DEPTH + 1024)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument(
        "--same-lineage", action="store_true", help="Permit added/removed route classification"
    )
    parser.add_argument("--output", type=Path, help="Write JSON to this file instead of stdout")
    args = parser.parse_args()
    result = compare_catalogs(
        load_document(args.before), load_document(args.after), same_lineage=args.same_lineage
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
