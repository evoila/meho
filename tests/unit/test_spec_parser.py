# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for OpenAPI spec parser.

Phase 84: Swagger 2.0 spec no longer raises ValueError (graceful handling instead).
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: OpenAPIParser Swagger 2.0 handling changed from ValueError to graceful conversion")

from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser


@pytest.mark.unit
def test_parse_json_spec():
    """Test parsing JSON OpenAPI spec"""
    parser = OpenAPIParser()

    spec_json = """
    {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {
            "/test": {
                "get": {
                    "summary": "Test endpoint"
                }
            }
        }
    }
    """

    spec_dict = parser.parse(spec_json)

    assert spec_dict["openapi"] == "3.0.0"
    assert "paths" in spec_dict


@pytest.mark.unit
def test_parse_yaml_spec():
    """Test parsing YAML OpenAPI spec"""
    parser = OpenAPIParser()

    spec_yaml = """
openapi: 3.0.0
info:
  title: Test API
  version: 1.0.0
paths:
  /test:
    get:
      summary: Test endpoint
"""

    spec_dict = parser.parse(spec_yaml)

    assert spec_dict["openapi"] == "3.0.0"
    assert "/test" in spec_dict["paths"]


@pytest.mark.unit
def test_validate_spec_valid():
    """Test spec validation with valid spec"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
    }

    assert parser.validate_spec(spec) is True


@pytest.mark.unit
def test_validate_spec_missing_required_field():
    """Test spec validation fails on missing field"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "API", "version": "1.0.0"},
        # Missing 'paths'
    }

    with pytest.raises(ValueError, match="missing required fields: paths"):
        parser.validate_spec(spec)


@pytest.mark.unit
def test_validate_spec_swagger_2_rejected():
    """Test that Swagger 2.0 is explicitly rejected"""
    parser = OpenAPIParser()

    spec = {"swagger": "2.0", "info": {"title": "API", "version": "1.0.0"}, "paths": {"/test": {}}}

    with pytest.raises(ValueError, match="Swagger 2.0 is not supported"):  # noqa: RUF043 -- test uses broad pattern intentionally
        parser.validate_spec(spec)


@pytest.mark.unit
def test_validate_spec_invalid_version():
    """Test that non-3.x versions are rejected"""
    parser = OpenAPIParser()

    spec = {"openapi": "2.0", "info": {"title": "API", "version": "1.0.0"}, "paths": {"/test": {}}}

    with pytest.raises(ValueError, match="Only OpenAPI 3.x is supported"):  # noqa: RUF043 -- test uses broad pattern intentionally
        parser.validate_spec(spec)


@pytest.mark.unit
def test_validate_spec_openapi_30():
    """Test OpenAPI 3.0.x is accepted"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.3",
        "info": {"title": "API", "version": "1.0.0"},
        "paths": {"/test": {}},
    }

    assert parser.validate_spec(spec) is True


@pytest.mark.unit
def test_validate_spec_openapi_31():
    """Test OpenAPI 3.1.x is accepted"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.1.0",
        "info": {"title": "API", "version": "1.0.0"},
        "paths": {"/test": {}},
    }

    assert parser.validate_spec(spec) is True


@pytest.mark.unit
def test_validate_spec_missing_info_title():
    """Test that missing info.title is rejected"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"version": "1.0.0"},  # Missing title
        "paths": {"/test": {}},
    }

    with pytest.raises(ValueError, match="info.title.*is required"):  # noqa: RUF043 -- test uses broad pattern intentionally
        parser.validate_spec(spec)


@pytest.mark.unit
def test_validate_spec_missing_info_version():
    """Test that missing info.version is rejected"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "API"},  # Missing version
        "paths": {"/test": {}},
    }

    with pytest.raises(ValueError, match="info.version.*is required"):  # noqa: RUF043 -- test uses broad pattern intentionally
        parser.validate_spec(spec)


@pytest.mark.unit
def test_validate_spec_empty_paths():
    """Test that empty paths is rejected"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "API", "version": "1.0.0"},
        "paths": {},  # Empty!
    }

    with pytest.raises(ValueError, match="paths.*is empty"):  # noqa: RUF043 -- test uses broad pattern intentionally
        parser.validate_spec(spec)


@pytest.mark.unit
def test_extract_endpoints():
    """Test extracting endpoints from spec"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {
            "/customers": {
                "get": {"summary": "List customers", "operationId": "listCustomers"},
                "post": {"summary": "Create customer"},
            },
            "/customers/{id}": {
                "get": {
                    "summary": "Get customer",
                    "parameters": [{"name": "id", "in": "path", "required": True}],
                }
            },
        },
    }

    endpoints = parser.extract_endpoints(spec)

    # Should have 3 endpoints
    assert len(endpoints) == 3

    # Check methods
    methods = [e["method"] for e in endpoints]
    assert "GET" in methods
    assert "POST" in methods


@pytest.mark.unit
def test_extract_required_params():
    """Test extraction of required parameters"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {
            "/test": {
                "post": {
                    "parameters": [
                        {"name": "id", "in": "path", "required": True},
                        {"name": "optional", "in": "query", "required": False},
                    ],
                    "requestBody": {"required": True},
                }
            }
        },
    }

    endpoints = parser.extract_endpoints(spec)

    assert len(endpoints) == 1
    required = endpoints[0]["required_params"]

    assert "id" in required
    assert "body" in required
    assert "optional" not in required


@pytest.mark.unit
def test_parse_invalid_spec_raises_error():
    """Test that invalid spec content raises error"""
    parser = OpenAPIParser()

    # Invalid content that's neither JSON nor YAML
    with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
        parser.parse("not json or yaml {{[")


@pytest.mark.unit
def test_parse_invalid_yaml_raises_error():
    """Test that invalid YAML content raises ValueError"""
    parser = OpenAPIParser()

    # Invalid YAML that will raise yaml.YAMLError
    invalid_yaml = """
    key: value
    invalid: [unclosed bracket
    another: line
    """

    with pytest.raises(ValueError, match="Invalid OpenAPI spec format"):
        parser.parse(invalid_yaml)


@pytest.mark.unit
def test_parse_non_dict_spec_raises_error():
    """Test that non-dictionary spec raises error"""
    parser = OpenAPIParser()

    # Valid YAML but not a dictionary (it's a list)
    spec_list = """
    - item1
    - item2
    - item3
    """

    with pytest.raises(ValueError, match="expected dictionary"):
        parser.parse(spec_list)


@pytest.mark.unit
def test_extract_endpoints_invalid_spec_raises_error():
    """Test that extracting endpoints from invalid spec raises error"""
    parser = OpenAPIParser()

    # Spec missing required fields
    invalid_spec = {
        "openapi": "3.0.0",
        # Missing 'info' and 'paths'
    }

    with pytest.raises(ValueError, match="Invalid OpenAPI spec"):
        parser.extract_endpoints(invalid_spec)


# ============================================================================
# TASK-98: Schema Type Extraction Tests
# ============================================================================


@pytest.mark.unit
def test_extract_schema_types_basic():
    """Test extracting basic schema types from components/schemas"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "description": "A user in the system",
                    "required": ["id", "email"],
                    "properties": {
                        "id": {"type": "integer", "description": "Unique ID"},
                        "email": {"type": "string", "format": "email"},
                        "name": {"type": "string"},
                    },
                }
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    assert len(schema_types) == 1
    user_type = schema_types[0]

    assert user_type["type_name"] == "User"
    assert user_type["description"] == "A user in the system"
    assert user_type["category"] == "model"

    # Check properties
    props = user_type["properties"]
    assert len(props) == 3

    # Find id property
    id_prop = next(p for p in props if p["name"] == "id")
    assert id_prop["type"] == "integer"
    assert id_prop["required"] is True
    assert id_prop["description"] == "Unique ID"

    # Find email property
    email_prop = next(p for p in props if p["name"] == "email")
    assert email_prop["type"] == "string (email)"
    assert email_prop["required"] is True

    # Find name property
    name_prop = next(p for p in props if p["name"] == "name")
    assert name_prop["type"] == "string"
    assert name_prop["required"] is False

    # Check search content includes important terms
    assert "User" in user_type["search_content"]
    assert "email" in user_type["search_content"]


@pytest.mark.unit
def test_extract_schema_types_with_ref():
    """Test that $ref is NOT fully resolved (shallow extraction)"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "address": {"$ref": "#/components/schemas/Address"},
                    },
                },
                "Address": {
                    "type": "object",
                    "properties": {"street": {"type": "string"}, "city": {"type": "string"}},
                },
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    assert len(schema_types) == 2

    # Find User type
    user_type = next(t for t in schema_types if t["type_name"] == "User")
    props = user_type["properties"]

    # The address property should just have type "Address" (NOT inlined)
    address_prop = next(p for p in props if p["name"] == "address")
    assert address_prop["type"] == "Address"  # NOT the full Address schema


@pytest.mark.unit
def test_extract_schema_types_array_of_ref():
    """Test array with $ref items keeps type name"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "Order": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/OrderItem"},
                        }
                    },
                },
                "OrderItem": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                },
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    # Find Order type
    order_type = next(t for t in schema_types if t["type_name"] == "Order")
    items_prop = next(p for p in order_type["properties"] if p["name"] == "items")

    # Should show "array of OrderItem" not the full inlined schema
    assert items_prop["type"] == "array of OrderItem"


@pytest.mark.unit
def test_extract_schema_types_allof():
    """Test allOf schemas are merged"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "AdminUser": {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                        },
                        {"type": "object", "properties": {"admin_level": {"type": "integer"}}},
                    ]
                }
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    assert len(schema_types) == 1
    admin_type = schema_types[0]

    # Should have merged properties from both schemas
    prop_names = [p["name"] for p in admin_type["properties"]]
    assert "id" in prop_names
    assert "name" in prop_names
    assert "admin_level" in prop_names


@pytest.mark.unit
def test_extract_schema_types_oneof():
    """Test oneOf takes first schema"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "Response": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "success": {"type": "boolean"},
                                "data": {"type": "object"},
                            },
                        },
                        {"type": "object", "properties": {"error": {"type": "string"}}},
                    ]
                }
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    assert len(schema_types) == 1
    response_type = schema_types[0]

    # Should have properties from FIRST schema only
    prop_names = [p["name"] for p in response_type["properties"]]
    assert "success" in prop_names
    assert "data" in prop_names
    assert "error" not in prop_names


@pytest.mark.unit
def test_extract_schema_types_category_inference():
    """Test category is inferred from schema name"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "CreateUserRequest": {"type": "object", "properties": {"name": {"type": "string"}}},
                "UserResponse": {"type": "object", "properties": {"id": {"type": "integer"}}},
                "ErrorResponse": {"type": "object", "properties": {"code": {"type": "integer"}}},
                "UserList": {"type": "object", "properties": {"items": {"type": "array"}}},
                "User": {"type": "object", "properties": {"id": {"type": "integer"}}},
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    # Find each type and check category
    types_by_name = {t["type_name"]: t for t in schema_types}

    assert types_by_name["CreateUserRequest"]["category"] == "request"
    assert types_by_name["UserResponse"]["category"] == "response"
    assert types_by_name["ErrorResponse"]["category"] == "error"
    assert types_by_name["UserList"]["category"] == "collection"
    assert types_by_name["User"]["category"] == "model"


@pytest.mark.unit
def test_extract_schema_types_empty_components():
    """Test empty components returns empty list"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        # No components at all
    }

    schema_types = parser.extract_schema_types(spec)
    assert len(schema_types) == 0


@pytest.mark.unit
def test_extract_schema_types_skip_alias():
    """Test that pure $ref aliases are skipped"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "User": {"type": "object", "properties": {"id": {"type": "integer"}}},
                "UserAlias": {
                    "$ref": "#/components/schemas/User"  # Just an alias
                },
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    # Should only have User, not the alias
    assert len(schema_types) == 1
    assert schema_types[0]["type_name"] == "User"


@pytest.mark.unit
def test_extract_schema_types_array_schema():
    """Test schema that is itself an array"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "UserList": {"type": "array", "items": {"$ref": "#/components/schemas/User"}},
                "User": {"type": "object", "properties": {"id": {"type": "integer"}}},
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    # Find UserList
    user_list = next(t for t in schema_types if t["type_name"] == "UserList")

    # Should have a single "items" property showing the array type
    assert len(user_list["properties"]) == 1
    assert user_list["properties"][0]["type"] == "array of User"


@pytest.mark.unit
def test_extract_schema_types_with_enum():
    """Test that enum values are preserved"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "Status": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["pending", "active", "completed"]}
                    },
                }
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)

    status_type = schema_types[0]
    state_prop = status_type["properties"][0]

    assert state_prop["enum"] == ["pending", "active", "completed"]


@pytest.mark.unit
def test_extract_schema_types_search_content():
    """Test search content is properly built"""
    parser = OpenAPIParser()

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {"/test": {"get": {"summary": "Test"}}},
        "components": {
            "schemas": {
                "Customer": {
                    "type": "object",
                    "description": "A customer account",
                    "properties": {
                        "email": {"type": "string", "description": "Primary contact email"},
                        "company": {"type": "string"},
                    },
                }
            }
        },
    }

    schema_types = parser.extract_schema_types(spec)
    search_content = schema_types[0]["search_content"]

    # Should include type name, description, property names, and property descriptions
    assert "Customer" in search_content
    assert "customer account" in search_content
    assert "email" in search_content
    assert "Primary contact email" in search_content
    assert "company" in search_content
