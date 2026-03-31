# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Schema formatting helpers for OpenAPI endpoint parameters.

This module provides functions to:
- Format required/optional parameters from endpoints
- Generate usage examples from endpoint schemas
- Summarize response and request body schemas
"""

from typing import Any


def format_required_params(endpoint: Any) -> dict[str, Any]:
    """
    Format required parameters with details.

    Extracts required parameters from:
    - Path parameters (always required)
    - Required query parameters
    - Request body (required for write operations)

    Args:
        endpoint: Endpoint descriptor with schema information

    Returns:
        Dict mapping parameter names to their details
    """
    required: dict[str, Any] = {}

    # Path params (always required if in path)
    if endpoint.path_params_schema:
        for param_name, param_schema in endpoint.path_params_schema.items():
            required[param_name] = {
                "in": "path",
                "type": param_schema.get("type", "string"),
                "description": param_schema.get("description", ""),
                "example": param_schema.get("example", f"<{param_name}>"),
            }

    # Required query params
    if endpoint.query_params_schema:
        for param_name, param_schema in endpoint.query_params_schema.items():
            if param_schema.get("required", False):
                required[param_name] = {
                    "in": "query",
                    "type": param_schema.get("type", "string"),
                    "description": param_schema.get("description", ""),
                    "example": param_schema.get("example", ""),
                }

    # Required body - if POST/PUT/PATCH has a body schema, it's required
    if endpoint.body_schema:
        required["body"] = {
            "in": "body",
            "schema": endpoint.body_schema,
            "description": "Request body",
        }

    return required


def format_optional_params(endpoint: Any) -> dict[str, Any]:
    """
    Format optional parameters.

    Extracts optional query parameters that are not marked as required.

    Args:
        endpoint: Endpoint descriptor with schema information

    Returns:
        Dict mapping parameter names to their details
    """
    optional: dict[str, Any] = {}

    # Optional query params
    if endpoint.query_params_schema:
        for param_name, param_schema in endpoint.query_params_schema.items():
            if not param_schema.get("required", False):
                optional[param_name] = {
                    "in": "query",
                    "type": param_schema.get("type", "string"),
                    "description": param_schema.get("description", ""),
                    "example": param_schema.get("example", ""),
                }

    # Note: If body_schema exists, it's in required_params (body is required for write ops)

    return optional


def generate_usage_example(endpoint: Any) -> dict[str, Any]:
    """
    Generate usage example for endpoint.

    Creates example parameter values based on the endpoint's schema.

    Args:
        endpoint: Endpoint descriptor with schema information

    Returns:
        Dict with path_params and query_params examples
    """
    example: dict[str, Any] = {}

    # Path params
    if endpoint.path_params_schema:
        example["path_params"] = {
            name: schema.get("example", f"<{name}>")
            for name, schema in endpoint.path_params_schema.items()
        }

    # Query params (include common useful ones that have examples)
    if endpoint.query_params_schema:
        query_examples = {}
        for name, schema in endpoint.query_params_schema.items():
            if schema.get("example"):
                query_examples[name] = schema["example"]
        if query_examples:
            example["query_params"] = query_examples

    return example


def summarize_response_schema(response_schema: dict[str, Any]) -> dict[str, Any]:
    """
    Summarize response schema so the agent can verify endpoint returns what's needed.

    TASK-90: This helps the agent reason about whether the endpoint is correct
    based on what it returns, not just its name.

    Args:
        response_schema: Full OpenAPI response schema (can be large)

    Returns:
        Summarized schema with key fields and types
    """
    if not response_schema:
        return {"note": "No response schema available"}

    # Handle OpenAPI response object structure
    # Usually has status codes like "200", "201", etc.
    summary: dict[str, Any] = {}

    # Check for success responses (2xx)
    # Note: OpenAPI standard uses string keys like "200", not integers
    for status_code in ["200", "201", "202"]:
        response = response_schema.get(status_code)
        if response is not None and isinstance(response, dict):
            content = response.get("content", {})
            json_content = content.get("application/json", {})
            schema = json_content.get("schema", {})

            if schema:
                summary = _extract_schema_summary(schema)
                summary["_status_code"] = str(status_code)
                break

    # If no structured response found, check for direct schema
    if not summary and isinstance(response_schema, dict):  # noqa: SIM102 -- readability preferred over collapse
        if "properties" in response_schema or "items" in response_schema:
            summary = _extract_schema_summary(response_schema)

    return summary if summary else {"note": "Response schema exists but format unknown"}


def _extract_schema_summary(schema: dict[str, Any]) -> dict[str, Any]:
    """
    Extract key information from an OpenAPI schema.

    Args:
        schema: OpenAPI schema object

    Returns:
        Summarized schema with type and field information
    """
    summary: dict[str, Any] = {}

    # Check if it's an array
    if schema.get("type") == "array":
        items = schema.get("items", {})
        summary["type"] = "array"
        summary["item_type"] = items.get("type", "object")
        # Get properties of array items
        if "properties" in items:
            summary["item_fields"] = list(items["properties"].keys())[:10]  # Limit to 10
            if len(items["properties"]) > 10:
                summary["item_fields"].append(f"... and {len(items['properties']) - 10} more")

    # Check if it's an object
    elif schema.get("type") == "object" or "properties" in schema:
        summary["type"] = "object"
        props = schema.get("properties", {})
        summary["fields"] = list(props.keys())[:10]  # Limit to 10
        if len(props) > 10:
            summary["fields"].append(f"... and {len(props) - 10} more")

    # Check for $ref (reference to another schema)
    elif "$ref" in schema:
        ref = schema["$ref"]
        # Extract schema name from ref like "#/components/schemas/VmInfo"
        summary["type"] = "reference"
        summary["schema_name"] = ref.split("/")[-1] if "/" in ref else ref

    else:
        summary["type"] = schema.get("type", "unknown")

    # Include description if available
    if schema.get("description"):
        summary["description"] = schema["description"][:200]  # Limit length

    return summary


def summarize_request_body_schema(body_schema: dict[str, Any]) -> dict[str, Any]:
    """
    Summarize request body schema for POST/PUT/PATCH endpoints.

    Helps the agent understand what data needs to be sent in the request body.

    Args:
        body_schema: OpenAPI request body schema

    Returns:
        Summary with required/optional fields and their types
    """
    if not body_schema:
        return {"note": "No request body schema available"}

    summary: dict[str, Any] = {}

    # Handle content-type wrapper (application/json)
    if "content" in body_schema:
        json_content = body_schema.get("content", {}).get("application/json", {})
        schema = json_content.get("schema", {})
    else:
        schema = body_schema

    # Extract fields
    if "properties" in schema:
        props = schema.get("properties", {})
        required_fields = schema.get("required", [])

        # Separate required and optional fields
        required_summary: dict[str, Any] = {}
        optional_summary: dict[str, Any] = {}

        for field_name, field_schema in list(props.items())[:15]:  # Limit to 15 fields
            field_info: dict[str, Any] = {
                "type": field_schema.get("type", "unknown"),
            }
            if field_schema.get("description"):
                field_info["description"] = field_schema["description"][:100]
            if field_schema.get("example"):
                field_info["example"] = field_schema["example"]
            if field_schema.get("enum"):
                field_info["allowed_values"] = field_schema["enum"][:5]  # Limit enum values

            if field_name in required_fields:
                required_summary[field_name] = field_info
            else:
                optional_summary[field_name] = field_info

        if required_summary:
            summary["required_fields"] = required_summary
        if optional_summary:
            summary["optional_fields"] = optional_summary

        if len(props) > 15:
            summary["note"] = f"... and {len(props) - 15} more fields"

    # Handle $ref
    elif "$ref" in schema:
        ref = schema["$ref"]
        summary["schema_name"] = ref.split("/")[-1] if "/" in ref else ref
        summary["type"] = "reference"

    # Handle array body
    elif schema.get("type") == "array":
        summary["type"] = "array"
        items = schema.get("items", {})
        if "properties" in items:
            summary["item_fields"] = list(items["properties"].keys())[:10]

    return summary if summary else {"note": "Request body schema exists but format unknown"}
