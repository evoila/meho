"""Tests for OpenAPI spec parser."""

from pathlib import Path

import pytest

from meho_claude.core.connectors.openapi_parser import parse_openapi_spec


FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"
PETSTORE_SPEC = str(FIXTURES_DIR / "petstore_openapi.yaml")


class TestParseOpenAPISpec:
    """Test parse_openapi_spec with petstore fixture."""

    def test_returns_list_of_operations(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        assert isinstance(ops, list)
        assert len(ops) == 5  # 3 GET + 1 POST + 1 DELETE

    def test_operation_connector_name(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        for op in ops:
            assert op.connector_name == "petstore"

    def test_trust_tier_read_for_get(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        get_ops = [o for o in ops if o.http_method == "GET"]
        assert len(get_ops) == 3
        for op in get_ops:
            assert op.trust_tier == "READ"

    def test_trust_tier_write_for_post(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        post_ops = [o for o in ops if o.http_method == "POST"]
        assert len(post_ops) == 1
        assert post_ops[0].trust_tier == "WRITE"

    def test_trust_tier_destructive_for_delete(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        del_ops = [o for o in ops if o.http_method == "DELETE"]
        assert len(del_ops) == 1
        assert del_ops[0].trust_tier == "DESTRUCTIVE"

    def test_operation_id_preserved(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        op_ids = {o.operation_id for o in ops}
        assert "listPets" in op_ids
        assert "createPet" in op_ids
        assert "getPet" in op_ids
        assert "getInventory" in op_ids

    def test_synthetic_operation_id_for_missing(self):
        """DELETE /pets/{petId} has no operationId -- parser should generate one."""
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        del_ops = [o for o in ops if o.http_method == "DELETE"]
        assert len(del_ops) == 1
        # Synthetic ID should be derived from method + path
        assert del_ops[0].operation_id != ""
        # Should be something like "delete_pets_petId"
        assert "delete" in del_ops[0].operation_id.lower()
        assert "pet" in del_ops[0].operation_id.lower()

    def test_url_template_extracted(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        assert ops_by_id["listPets"].url_template == "/pets"
        assert ops_by_id["getPet"].url_template == "/pets/{petId}"

    def test_http_method_extracted(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        assert ops_by_id["listPets"].http_method == "GET"
        assert ops_by_id["createPet"].http_method == "POST"

    def test_display_name_from_summary(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        assert ops_by_id["listPets"].display_name == "List all pets"

    def test_description_extracted(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        assert "filtering" in ops_by_id["listPets"].description.lower()

    def test_tags_extracted(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        assert "pets" in ops_by_id["listPets"].tags
        assert "store" in ops_by_id["getInventory"].tags

    def test_input_schema_has_parameters(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        schema = ops_by_id["listPets"].input_schema
        assert "parameters" in schema
        param_names = [p["name"] for p in schema["parameters"]]
        assert "limit" in param_names
        assert "status" in param_names

    def test_input_schema_has_body_for_post(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        schema = ops_by_id["createPet"].input_schema
        assert "body" in schema

    def test_output_schema_extracted(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        # listPets should have a response schema
        assert ops_by_id["listPets"].output_schema != {}

    def test_path_parameters_extracted(self):
        ops = parse_openapi_spec(PETSTORE_SPEC, "petstore")
        ops_by_id = {o.operation_id: o for o in ops}
        schema = ops_by_id["getPet"].input_schema
        assert "parameters" in schema
        param_names = [p["name"] for p in schema["parameters"]]
        assert "petId" in param_names
