"""OpenAPI spec parsing and operation extraction.

Parses OpenAPI 3.0/3.1 specs (YAML or JSON) into a list of Operation models.
Uses prance for $ref resolution and walks paths/methods to build operations.
"""

from __future__ import annotations

import re

import prance

from meho_claude.core.connectors.models import Operation

# HTTP method -> trust tier mapping per CONTEXT.md decision
_TRUST_MAP: dict[str, str] = {
    "get": "READ",
    "head": "READ",
    "options": "READ",
    "post": "WRITE",
    "put": "WRITE",
    "patch": "WRITE",
    "delete": "DESTRUCTIVE",
}

# HTTP methods we care about (skip 'trace', 'servers', 'parameters', etc.)
_HTTP_METHODS = frozenset(_TRUST_MAP.keys())


def parse_openapi_spec(spec_source: str, connector_name: str) -> list[Operation]:
    """Parse an OpenAPI spec into a list of Operation models.

    Args:
        spec_source: URL (http/https) or local file path to the spec.
        connector_name: Name of the connector these operations belong to.

    Returns:
        List of Operation models extracted from the spec.
    """
    # prance handles both URLs and file paths
    parser = prance.ResolvingParser(spec_source, strict=False)
    spec = parser.specification

    operations: list[Operation] = []

    paths = spec.get("paths", {})
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        # Collect path-level parameters (shared across all methods)
        path_params = path_item.get("parameters", [])

        for method, op_data in path_item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(op_data, dict):
                continue

            method_upper = method.upper()
            trust_tier = _method_to_trust_tier(method)

            # Operation ID: use explicit or generate synthetic
            operation_id = op_data.get("operationId") or _sanitize_operation_id(method, path)

            # Display name from summary, fallback to operation_id
            display_name = op_data.get("summary", operation_id)

            # Description
            description = op_data.get("description", "")

            # Tags
            tags = op_data.get("tags", [])

            # URL template is the path itself
            url_template = path

            # Input schema: combine path-level + operation-level parameters + request body
            all_params = list(path_params) + op_data.get("parameters", [])
            input_schema = _extract_input_schema(all_params, op_data)

            # Output schema from responses
            output_schema = _extract_output_schema(op_data)

            operations.append(
                Operation(
                    connector_name=connector_name,
                    operation_id=operation_id,
                    display_name=display_name,
                    description=description,
                    trust_tier=trust_tier,
                    http_method=method_upper,
                    url_template=url_template,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    tags=tags,
                )
            )

    # Discard resolved spec immediately (memory management per pitfall 6)
    del parser
    del spec

    return operations


def _method_to_trust_tier(method: str) -> str:
    """Map an HTTP method to a trust tier."""
    return _TRUST_MAP.get(method.lower(), "READ")


def _sanitize_operation_id(method: str, path: str) -> str:
    """Generate a clean operation ID from method + path when operationId is missing.

    Examples:
        ("delete", "/pets/{petId}") -> "delete_pets_petId"
        ("get", "/store/inventory") -> "get_store_inventory"
    """
    # Remove leading slash, replace {param} braces, replace non-alphanumeric
    cleaned = path.lstrip("/")
    cleaned = cleaned.replace("{", "").replace("}", "")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", cleaned)
    cleaned = cleaned.strip("_")
    return f"{method.lower()}_{cleaned}"


def _extract_input_schema(
    parameters: list[dict],
    op_data: dict,
) -> dict:
    """Combine parameters + requestBody into a unified input schema.

    Returns:
        Dict with "parameters" list and optionally "body" schema.
    """
    result: dict = {}

    if parameters:
        param_list = []
        for param in parameters:
            p = {
                "name": param.get("name", ""),
                "in": param.get("in", ""),
                "required": param.get("required", False),
            }
            schema = param.get("schema", {})
            if schema:
                p["schema"] = schema
            param_list.append(p)
        result["parameters"] = param_list

    # Request body
    request_body = op_data.get("requestBody", {})
    if request_body:
        content = request_body.get("content", {})
        # Pick first media type (usually application/json)
        for media_type, media_obj in content.items():
            body_schema = media_obj.get("schema", {})
            if body_schema:
                result["body"] = body_schema
                break

    return result


def _extract_output_schema(op_data: dict) -> dict:
    """Pull schema from successful response codes (200, 201, 202, 204)."""
    responses = op_data.get("responses", {})

    for code in ("200", "201", "202", "204"):
        response = responses.get(code, {})
        if not isinstance(response, dict):
            continue
        content = response.get("content", {})
        for media_type, media_obj in content.items():
            schema = media_obj.get("schema", {})
            if schema:
                return schema

    return {}
