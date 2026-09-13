"""Contract-level tests for the offline VCF OpenAPI/Swagger catalog comparator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diff_vcf_api_contract_catalogs.py"
SPEC = importlib.util.spec_from_file_location("vcf_catalog_diff", SCRIPT)
assert SPEC and SPEC.loader
catalog_diff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog_diff)


def _document(*, child_type: str = "string", description: str = "first") -> dict[object, object]:
    return {
        "openapi": "3.0.0",
        "servers": [{"url": "/api"}],
        "security": [{"bearerAuth": []}],
        "components": {
            "schemas": {
                "Request": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Child"}},
                },
                "Child": {"type": "object", "properties": {"value": {"type": child_type}}},
            }
        },
        "paths": {
            "/things/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "post": {
                    "description": description,
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Request"}}
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
    }


def test_comparator_reports_transitive_schema_and_inherited_contract_changes() -> None:
    before = _document()
    after = _document(child_type="integer")
    after["security"] = [{"basicAuth": []}]

    result = catalog_diff.compare_catalogs(before, after, same_lineage=True)

    assert result["comparison_status"] == "same-artifact-lineage"
    assert result["changed"] == [
        {"operation_id": "POST:/things/{id}", "fields": ["request", "security"]}
    ]
    assert result["added"] == []
    assert result["removed"] == []


def test_comparator_separates_description_churn_from_contract_change() -> None:
    before = _document(description="first")
    after = _document(description="rewritten prose")

    result = catalog_diff.compare_catalogs(before, after, same_lineage=True)

    assert result["changed"] == []
    assert result["description_only"] == ["POST:/things/{id}"]


def test_comparator_does_not_infer_removals_when_artifact_lineage_is_unknown() -> None:
    before = _document()
    after = {"openapi": "3.0.0", "paths": {}}

    result = catalog_diff.compare_catalogs(before, after, same_lineage=False)

    assert result["comparison_status"] == "artifact-lineage-unknown"
    assert result["removed"] == []


def test_comparator_preserves_schema_properties_named_description() -> None:
    before = _document()
    after = _document()
    before["components"]["schemas"]["Child"]["properties"] = {"description": {"type": "string"}}
    after["components"]["schemas"]["Child"]["properties"] = {"description": {"type": "integer"}}

    result = catalog_diff.compare_catalogs(before, after, same_lineage=True)

    assert result["changed"] == [{"operation_id": "POST:/things/{id}", "fields": ["request"]}]


def test_comparator_expands_effective_security_scheme_and_fails_closed_on_external_ref() -> None:
    before = _document()
    after = _document()
    before["components"]["securitySchemes"] = {"bearerAuth": {"type": "http", "scheme": "bearer"}}
    after["components"]["securitySchemes"] = {
        "bearerAuth": {"type": "apiKey", "in": "header", "name": "X-Key"}
    }
    changed = catalog_diff.compare_catalogs(before, after, same_lineage=True)
    assert changed["changed"] == [{"operation_id": "POST:/things/{id}", "fields": ["security"]}]

    after["paths"]["/things/{id}"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ] = {"$ref": "other.yaml#/Request"}
    unresolved = catalog_diff.compare_catalogs(before, after, same_lineage=True)
    assert unresolved["comparison_status"] == "external-reference-unresolved"


def test_comparator_inherits_path_level_servers() -> None:
    before = _document()
    after = _document()
    before["paths"]["/things/{id}"]["servers"] = [{"url": "https://one.example"}]
    after["paths"]["/things/{id}"]["servers"] = [{"url": "https://two.example"}]

    result = catalog_diff.compare_catalogs(before, after, same_lineage=True)

    assert result["changed"] == [{"operation_id": "POST:/things/{id}", "fields": ["servers"]}]
