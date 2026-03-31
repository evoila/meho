# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
OpenAPI specification parser.

Parses OpenAPI 3.x and Swagger 2.0 specs and extracts endpoint information.
Swagger 2.0 support is primarily for Kubernetes /openapi/v2 endpoint compatibility.
"""

import contextlib
import json
from typing import Any, cast

import yaml

from meho_app.core.otel import get_logger


class OpenAPIParser:
    """Parse OpenAPI 3.x and Swagger 2.0 specifications"""

    def parse(self, spec_content: str) -> dict[str, Any]:
        """
        Parse OpenAPI spec from JSON or YAML.

        Args:
            spec_content: Spec as string (JSON or YAML)

        Returns:
            Parsed spec as dictionary

        Raises:
            ValueError: If content is not valid JSON/YAML or not a dictionary
        """
        result = None

        # Try JSON first
        with contextlib.suppress(json.JSONDecodeError):
            result = json.loads(spec_content)

        # Try YAML if JSON failed
        if result is None:
            try:
                result = yaml.safe_load(spec_content)
            except yaml.YAMLError as e:
                raise ValueError(f"Invalid OpenAPI spec format: {e}") from e

        # Validate result is a dictionary (OpenAPI specs must be objects, not scalars)
        if not isinstance(result, dict):
            raise ValueError(
                f"Invalid OpenAPI spec: expected dictionary, got {type(result).__name__}"
            )

        return result

    def validate_spec(self, spec_dict: dict[str, Any]) -> bool:
        """
        Validate OpenAPI spec structure and version.

        Supports: OpenAPI 3.0.x, 3.1.x, Swagger 2.0 (for K8s compatibility)

        Returns:
            True if valid, False otherwise

        Raises:
            ValueError: With detailed error message if validation fails
        """
        # Check for Swagger 2.0 (supported for K8s /openapi/v2 compatibility)
        if "swagger" in spec_dict:
            return self._validate_swagger_2(spec_dict)

        # Check required top-level fields (for OpenAPI 3.x)
        required_fields = ["openapi", "info", "paths"]
        missing_fields = [field for field in required_fields if field not in spec_dict]

        if missing_fields:
            raise ValueError(
                f"Invalid OpenAPI spec: missing required fields: {', '.join(missing_fields)}"
            )

        # Validate OpenAPI version
        openapi_version = spec_dict.get("openapi", "")

        # Validate OpenAPI 3.x
        if not openapi_version.startswith("3."):
            raise ValueError(
                f"Only OpenAPI 3.x is supported. Found version: {openapi_version}. "
                f"Supported versions: 3.0.x, 3.1.x"
            )

        # Parse version to check major.minor
        try:
            version_parts = openapi_version.split(".")
            major = int(version_parts[0])
            minor = int(version_parts[1]) if len(version_parts) > 1 else 0

            if major != 3:
                raise ValueError(f"Only OpenAPI 3.x is supported. Found version: {openapi_version}")

            if minor > 1:
                # Future versions (3.2+) - warn but allow
                logger = get_logger(__name__)
                logger.warning(
                    f"OpenAPI version {openapi_version} is newer than tested versions (3.0, 3.1). "
                    f"Parsing may not work correctly."
                )
        except (ValueError, IndexError):
            raise ValueError(
                f"Invalid OpenAPI version format: {openapi_version}. Expected format: 3.x.x"
            ) from None

        # Validate info object
        info = spec_dict.get("info", {})
        if not isinstance(info, dict):
            raise ValueError("Invalid OpenAPI spec: 'info' must be an object")

        if "title" not in info:
            raise ValueError("Invalid OpenAPI spec: 'info.title' is required")

        if "version" not in info:
            raise ValueError("Invalid OpenAPI spec: 'info.version' is required")

        # Validate paths object
        paths = spec_dict.get("paths", {})
        if not isinstance(paths, dict):
            raise ValueError("Invalid OpenAPI spec: 'paths' must be an object")

        if len(paths) == 0:
            raise ValueError(
                "Invalid OpenAPI spec: 'paths' is empty. Spec must contain at least one endpoint."
            )

        return True

    def _validate_swagger_2(self, spec_dict: dict[str, Any]) -> bool:
        """
        Validate Swagger 2.0 spec structure.

        Swagger 2.0 is supported for Kubernetes /openapi/v2 compatibility.
        """
        swagger_version = spec_dict.get("swagger", "")

        if not swagger_version.startswith("2."):
            raise ValueError(
                f"Unsupported Swagger version: {swagger_version}. Only Swagger 2.0 is supported."
            )

        # Check required fields for Swagger 2.0
        required_fields = ["swagger", "info", "paths"]
        missing_fields = [field for field in required_fields if field not in spec_dict]

        if missing_fields:
            raise ValueError(
                f"Invalid Swagger spec: missing required fields: {', '.join(missing_fields)}"
            )

        # Validate info object
        info = spec_dict.get("info", {})
        if not isinstance(info, dict):
            raise ValueError("Invalid Swagger spec: 'info' must be an object")

        if "title" not in info:
            raise ValueError("Invalid Swagger spec: 'info.title' is required")

        if "version" not in info:
            raise ValueError("Invalid Swagger spec: 'info.version' is required")

        # Validate paths object
        paths = spec_dict.get("paths", {})
        if not isinstance(paths, dict):
            raise ValueError("Invalid Swagger spec: 'paths' must be an object")

        # For K8s, paths can be very large, allow empty for flexibility
        # but log a warning
        if len(paths) == 0:
            logger = get_logger(__name__)
            logger.warning("Swagger spec has empty 'paths' object")

        return True

    def is_swagger_2(self, spec_dict: dict[str, Any]) -> bool:
        """Check if spec is Swagger 2.0 format."""
        return "swagger" in spec_dict and spec_dict.get("swagger", "").startswith("2.")

    def _resolve_ref(
        self, schema: dict[str, Any], spec_dict: dict[str, Any], max_depth: int = 10
    ) -> dict[str, Any]:
        """
        Resolve $ref references in OpenAPI schemas.

        Args:
            schema: Schema that might contain $ref
            spec_dict: Full OpenAPI spec dictionary
            max_depth: Maximum recursion depth to prevent infinite loops

        Returns:
            Dereferenced schema
        """
        if max_depth <= 0:
            # Prevent infinite recursion
            return schema

        if not isinstance(schema, dict):
            return schema

        # Check if this schema has a $ref
        if "$ref" in schema:
            ref_path = schema["$ref"]

            # Only handle internal references (starting with #/)
            if not ref_path.startswith("#/"):
                return schema

            # Parse the reference path (e.g., #/components/schemas/Avn)
            # Remove leading #/ and split by /
            path_parts = ref_path[2:].split("/")

            # Navigate to the referenced schema
            current = spec_dict
            try:
                for part in path_parts:
                    current = current[part]

                # Recursively resolve in case the referenced schema also has refs
                if isinstance(current, dict):
                    return self._resolve_ref(current, spec_dict, max_depth - 1)
                return current
            except (KeyError, TypeError):
                # Reference not found, return original schema
                return schema

        # Recursively resolve refs in nested schemas
        resolved: dict[str, Any] = {}
        for key, value in schema.items():
            if isinstance(value, dict):
                resolved[key] = self._resolve_ref(value, spec_dict, max_depth - 1)
            elif isinstance(value, list):
                resolved[key] = cast(
                    Any,
                    [
                        self._resolve_ref(item, spec_dict, max_depth - 1)
                        if isinstance(item, dict)
                        else item
                        for item in value
                    ],
                )
            else:
                resolved[key] = value

        return resolved

    def extract_endpoints(self, spec_dict: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract all endpoints from OpenAPI spec.

        Handles both OpenAPI 3.x and Swagger 2.0 formats.

        Returns:
            List of endpoint descriptor dicts
        """
        if not self.validate_spec(spec_dict):
            raise ValueError("Invalid OpenAPI spec")

        is_swagger = self.is_swagger_2(spec_dict)
        endpoints = []
        paths = spec_dict.get("paths", {})

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            for method in ["get", "post", "put", "delete", "patch"]:
                if method not in path_item:
                    continue

                operation = path_item[method]
                if not isinstance(operation, dict):
                    continue

                endpoints.append(
                    {
                        "method": method.upper(),
                        "path": path,
                        "operation_id": operation.get("operationId"),
                        "summary": operation.get("summary", ""),
                        "description": operation.get("description", ""),
                        "tags": operation.get("tags", []),
                        "required_params": self._extract_required_params(operation, is_swagger),
                        "path_params_schema": self._extract_path_params_schema(
                            operation, spec_dict
                        ),
                        "query_params_schema": self._extract_query_params_schema(
                            operation, spec_dict
                        ),
                        "body_schema": self._extract_body_schema(operation, spec_dict, is_swagger),
                        "response_schema": self._extract_response_schema(
                            operation, spec_dict, is_swagger
                        ),
                        "parameter_metadata": self._extract_parameter_metadata(
                            operation, spec_dict, is_swagger
                        ),
                    }
                )

        return endpoints

    def _extract_required_params(
        self, operation: dict[str, Any], is_swagger: bool = False
    ) -> list[str]:
        """Extract required parameter names"""
        required = []
        for param in operation.get("parameters", []):
            if not isinstance(param, dict):
                continue
            if param.get("required", False):
                required.append(param.get("name", ""))

        # OpenAPI 3.x: check requestBody
        if not is_swagger and operation.get("requestBody", {}).get("required", False):
            required.append("body")

        return [r for r in required if r]  # Filter empty names

    def _extract_parameter_metadata(
        self, operation: dict[str, Any], spec_dict: dict[str, Any], is_swagger: bool = False
    ) -> dict[str, Any]:
        """
        Extract explicit parameter metadata for LLM guidance.

        Creates a structured format that clearly shows what's required vs optional,
        making it easy for LLMs to understand endpoint requirements.

        Handles both OpenAPI 3.x and Swagger 2.0 formats.
        """
        metadata: dict[str, Any] = {
            "path_params": {"required": [], "optional": []},
            "query_params": {"required": [], "optional": []},
            "header_params": {"required": [], "optional": []},
            "body": {"required": False, "required_fields": [], "optional_fields": []},
        }

        # Extract path, query, header, and body parameters
        for param in operation.get("parameters", []):
            if not isinstance(param, dict):
                continue

            param_name = param.get("name")
            param_in = param.get("in")
            is_required = param.get("required", False)

            if not param_name or not param_in:
                continue

            # Get type - Swagger 2.0 uses 'type' directly, OpenAPI 3.x uses 'schema.type'
            if is_swagger:
                param_type = param.get("type", "string")
            else:
                param_type = param.get("schema", {}).get("type", "string")

            param_info = {
                "name": param_name,
                "type": param_type,
                "description": param.get("description", ""),
            }

            if param_in == "path":
                target = metadata["path_params"]
            elif param_in == "query":
                target = metadata["query_params"]
            elif param_in == "header":
                target = metadata["header_params"]
            elif param_in == "body" and is_swagger:
                # Swagger 2.0: body is in parameters
                metadata["body"]["required"] = is_required
                body_schema = param.get("schema", {})
                resolved_schema = self._resolve_ref(body_schema, spec_dict)
                self._extract_body_fields(resolved_schema, metadata, spec_dict)
                continue
            else:
                continue

            if is_required:
                target["required"].append(param_info)
            else:
                target["optional"].append(param_info)

        # OpenAPI 3.x: Extract body requirements from requestBody
        if not is_swagger:
            request_body = operation.get("requestBody", {})
            if request_body:
                metadata["body"]["required"] = request_body.get("required", False)

                # Extract required/optional fields from body schema
                content = request_body.get("content", {})
                for content_type in ["application/json", "*/*"]:
                    if content_type in content:
                        body_schema = content[content_type].get("schema", {})
                        resolved_schema = self._resolve_ref(body_schema, spec_dict)
                        self._extract_body_fields(resolved_schema, metadata, spec_dict)
                        break

        return metadata

    def _extract_body_fields(
        self, resolved_schema: dict[str, Any], metadata: dict[str, Any], spec_dict: dict[str, Any]
    ) -> None:
        """Extract required and optional fields from a body schema."""
        if not isinstance(resolved_schema, dict):
            return

        if resolved_schema.get("type") == "object":
            required_fields = resolved_schema.get("required", [])
            all_properties = resolved_schema.get("properties", {})

            for prop_name, prop_schema in all_properties.items():
                if not isinstance(prop_schema, dict):
                    continue
                prop_info = {
                    "name": prop_name,
                    "type": prop_schema.get("type", "any"),
                    "description": prop_schema.get("description", ""),
                }

                if prop_name in required_fields:
                    metadata["body"]["required_fields"].append(prop_info)
                else:
                    metadata["body"]["optional_fields"].append(prop_info)

    def _extract_path_params_schema(
        self, operation: dict[str, Any], spec_dict: dict[str, Any]
    ) -> dict[str, Any]:
        """Extract path parameters schema from OpenAPI operation."""
        schema: dict[str, Any] = {}

        for param in operation.get("parameters", []):
            if param.get("in") == "path":
                param_name = param.get("name")
                if param_name:
                    param_schema = param.get("schema", {"type": "string"})
                    schema[param_name] = self._resolve_ref(param_schema, spec_dict)

        return schema

    def _extract_query_params_schema(
        self, operation: dict[str, Any], spec_dict: dict[str, Any]
    ) -> dict[str, Any]:
        """Extract query parameters schema from OpenAPI operation."""
        schema: dict[str, Any] = {}

        for param in operation.get("parameters", []):
            if param.get("in") == "query":
                param_name = param.get("name")
                if param_name:
                    param_schema = param.get("schema", {"type": "string"})
                    schema[param_name] = self._resolve_ref(param_schema, spec_dict)

        return schema

    def _extract_body_schema(
        self, operation: dict[str, Any], spec_dict: dict[str, Any], is_swagger: bool = False
    ) -> dict[str, Any]:
        """
        Extract request body schema from OpenAPI operation.

        Handles both OpenAPI 3.x (requestBody) and Swagger 2.0 (body parameter).
        """
        # Swagger 2.0: body is in parameters array
        if is_swagger:
            for param in operation.get("parameters", []):
                if not isinstance(param, dict):
                    continue
                if param.get("in") == "body":
                    schema = param.get("schema", {})
                    if schema and isinstance(schema, dict):
                        return self._resolve_ref(schema, spec_dict)
            return {}

        # OpenAPI 3.x: use requestBody
        request_body = operation.get("requestBody", {})

        if not request_body:
            return {}

        content = request_body.get("content", {})

        # Try common content types
        for content_type in ["application/json", "*/*", "application/vnd.api+json"]:
            if content_type not in content:
                continue

            media_type = content[content_type]
            schema = media_type.get("schema", {})

            if schema and isinstance(schema, dict):
                return self._resolve_ref(schema, spec_dict)

        return {}

    def _extract_response_schema(
        self, operation: dict[str, Any], spec_dict: dict[str, Any], is_swagger: bool = False
    ) -> dict[str, Any]:
        """
        Extract response schema from OpenAPI operation.

        Handles both OpenAPI 3.x and Swagger 2.0 formats.
        """
        responses = operation.get("responses", {})

        # Try common success status codes
        for status_code in ["200", "201", "default"]:
            if status_code not in responses:
                continue

            response = responses[status_code]
            if not isinstance(response, dict):
                continue

            # Swagger 2.0: schema is directly under response
            if is_swagger:
                schema = response.get("schema", {})
                if schema and isinstance(schema, dict):
                    return self._resolve_ref(schema, spec_dict)
                continue

            # OpenAPI 3.x: schema is under content
            content = response.get("content", {})

            # Try common content types
            for content_type in ["application/json", "*/*", "application/vnd.api+json"]:
                if content_type not in content:
                    continue

                media_type = content[content_type]
                schema = media_type.get("schema", {})

                if schema and isinstance(schema, dict):
                    return self._resolve_ref(schema, spec_dict)

        return {}

    # ========================================================================
    # Schema Extraction for Type Search
    # ========================================================================

    def extract_schema_types(self, spec_dict: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract schema definitions from OpenAPI components/schemas.

        Handles:
        - OpenAPI 3.x: components/schemas
        - OpenAPI 2.x (Swagger): definitions

        Returns:
            List of schema type dictionaries ready for storage
        """
        schema_types: list[dict[str, Any]] = []

        # OpenAPI 3.x
        schemas = spec_dict.get("components", {}).get("schemas", {})

        # Fallback: OpenAPI 2.x (Swagger)
        if not schemas:
            schemas = spec_dict.get("definitions", {})

        for schema_name, schema_def in schemas.items():
            if not isinstance(schema_def, dict):
                continue

            # Skip if it's just a reference (alias)
            if "$ref" in schema_def and len(schema_def) == 1:
                continue

            # Handle allOf/oneOf/anyOf
            actual_schema = self._unwrap_composition(schema_def)

            # Extract properties (SHALLOW - keep type names)
            properties = self._extract_type_properties_shallow(actual_schema)

            # Build description
            description = schema_def.get("description", "")
            if not description:
                description = f"Schema: {schema_name}"

            # Determine category
            category = self._infer_schema_category(schema_name, schema_def)

            # Build search content for BM25
            search_content = self._build_schema_search_content(schema_name, description, properties)

            schema_types.append(
                {
                    "type_name": schema_name,
                    "description": description,
                    "category": category,
                    "properties": properties,
                    "search_content": search_content,
                }
            )

        return schema_types

    def _unwrap_composition(self, schema_def: dict[str, Any]) -> dict[str, Any]:
        """Handle allOf/oneOf/anyOf by extracting the primary schema."""
        if not isinstance(schema_def, dict):
            return {}

        if "allOf" in schema_def:
            # Merge all schemas in allOf
            merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            for sub_schema in schema_def.get("allOf", []):
                if isinstance(sub_schema, dict):
                    if "properties" in sub_schema:
                        merged["properties"].update(sub_schema["properties"])
                    if "required" in sub_schema:
                        merged["required"].extend(sub_schema.get("required", []))
            return merged

        if "oneOf" in schema_def:
            one_of = schema_def.get("oneOf", [])
            return one_of[0] if one_of and isinstance(one_of[0], dict) else {}

        if "anyOf" in schema_def:
            any_of = schema_def.get("anyOf", [])
            return any_of[0] if any_of and isinstance(any_of[0], dict) else {}

        return schema_def

    def _extract_type_properties_shallow(self, schema_def: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract properties WITHOUT fully resolving $refs."""
        if not isinstance(schema_def, dict):
            return []

        properties: list[dict[str, Any]] = []
        required_fields = set(schema_def.get("required", []))

        schema_type = schema_def.get("type", "object")

        if schema_type == "object":
            for prop_name, prop_def in schema_def.get("properties", {}).items():
                if not isinstance(prop_def, dict):
                    continue

                prop_entry: dict[str, Any] = {
                    "name": prop_name,
                    "type": self._get_type_name_shallow(prop_def),
                    "required": prop_name in required_fields,
                }

                if prop_def.get("description"):
                    prop_entry["description"] = prop_def["description"]
                if prop_def.get("format"):
                    prop_entry["format"] = prop_def["format"]
                if prop_def.get("enum"):
                    prop_entry["enum"] = prop_def["enum"]

                properties.append(prop_entry)

        elif schema_type == "array":
            items = schema_def.get("items", {})
            item_type = self._get_type_name_shallow(items) if isinstance(items, dict) else "any"
            properties.append(
                {
                    "name": "items",
                    "type": f"array of {item_type}",
                    "required": True,
                    "description": "Array items",
                }
            )

        return properties

    def _get_type_name_shallow(self, prop_def: dict[str, Any]) -> str:
        """Get type name WITHOUT resolving $ref."""
        if not isinstance(prop_def, dict):
            return "any"

        # Handle $ref - extract just the name
        if "$ref" in prop_def:
            ref = prop_def["$ref"]
            return str(ref).split("/")[-1]

        prop_type = str(prop_def.get("type", "any"))

        # Handle array
        if prop_type == "array":
            items = prop_def.get("items", {})
            if isinstance(items, dict):
                item_type = self._get_type_name_shallow(items)
                return f"array of {item_type}"
            return "array of any"

        # Handle format
        if prop_def.get("format"):
            return f"{prop_type} ({prop_def['format']})"

        return prop_type

    def _infer_schema_category(self, name: str, schema_def: dict[str, Any]) -> str:
        """Infer a category for the schema based on naming conventions."""
        name_lower = name.lower()

        if any(x in name_lower for x in ["error", "fault", "exception", "problem"]):
            return "error"
        if any(x in name_lower for x in ["request", "input", "create", "update", "patch"]):
            return "request"
        if any(x in name_lower for x in ["response", "output", "result"]):
            return "response"
        if any(x in name_lower for x in ["list", "array", "collection", "page"]):
            return "collection"

        return "model"

    def _build_schema_search_content(
        self, name: str, description: str, properties: list[dict[str, Any]]
    ) -> str:
        """Build search content for BM25 indexing."""
        parts = [name, description]
        for prop in properties:
            parts.append(prop.get("name", ""))
            parts.append(prop.get("type", ""))
            if prop.get("description"):
                parts.append(prop["description"])
        return " ".join(filter(None, parts))
