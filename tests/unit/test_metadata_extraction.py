# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for metadata extraction.
"""

from meho_app.modules.knowledge.metadata_extraction import MetadataExtractor
from meho_app.modules.knowledge.schemas import ContentType


def test_extract_endpoint_path():
    """Test endpoint extraction from text"""
    extractor = MetadataExtractor()

    text = "The GET /v1/roles endpoint returns a list of roles."
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.endpoint_path == "/v1/roles"
    assert metadata.http_method == "GET"
    assert metadata.resource_type == "roles"


def test_extract_endpoint_with_version():
    """Test endpoint extraction with version numbers"""
    extractor = MetadataExtractor()

    text = "Call the /api/v2/users/{id} endpoint to get user details."
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.endpoint_path == "/api/v2/users/{id}"
    assert metadata.resource_type == "users"


def test_detect_json_example():
    """Test JSON example detection"""
    extractor = MetadataExtractor()

    text = """
    Example response:
    {
      "elements": [
        {"id": "123", "name": "ADMIN"}
      ]
    }
    """

    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.content_type == ContentType.EXAMPLE_JSON
    assert metadata.has_json_example is True
    assert metadata.programming_language == "json"


def test_detect_code_example():
    """Test code example detection"""
    extractor = MetadataExtractor()

    text = """
    Example Python code:
    ```python
    import requests
    response = requests.get("/v1/roles")
    ```
    """

    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.content_type == ContentType.EXAMPLE_CODE
    assert metadata.has_code_example is True


def test_extract_keywords():
    """Test keyword extraction"""
    extractor = MetadataExtractor()

    text = 'The roles include "ADMIN", "OPERATOR", and "VIEWER".'
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert "ADMIN" in metadata.keywords
    assert "OPERATOR" in metadata.keywords
    assert "VIEWER" in metadata.keywords


def test_chapter_extraction():
    """Test chapter extraction from context"""
    extractor = MetadataExtractor()

    context = {"heading_stack": ["API Reference", "Roles Management", "GET /v1/roles"]}

    metadata = extractor.extract_metadata("Some text", "test.md", 0, context)

    assert metadata.chapter == "API Reference"
    assert metadata.section == "Roles Management"
    assert metadata.heading_hierarchy == ["API Reference", "Roles Management", "GET /v1/roles"]


def test_detect_table_content():
    """Test table detection"""
    extractor = MetadataExtractor()

    text = """| Name | Description |
|------|-------------|
| ADMIN | Administrator |
| VIEWER | Read-only |"""

    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.has_table is True
    assert metadata.content_type == ContentType.TABLE


def test_detect_parameters_content():
    """Test parameter detection"""
    extractor = MetadataExtractor()

    text = """Query Parameters:
- limit: Maximum number of results
- offset: Pagination offset
- filter: Filter expression"""

    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.content_type == ContentType.PARAMETERS


def test_detect_overview_content():
    """Test overview detection"""
    extractor = MetadataExtractor()

    text = "This overview describes the Roles Management API and its core functionality."
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.content_type == ContentType.OVERVIEW


def test_extract_http_method():
    """Test HTTP method extraction"""
    extractor = MetadataExtractor()

    text = "Use POST to create a new user."
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.http_method == "POST"


def test_extract_response_codes():
    """Test HTTP response code extraction"""
    extractor = MetadataExtractor()

    text = "Returns 200 on success, 404 if not found, or 500 on server error."
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert 200 in metadata.response_codes
    assert 404 in metadata.response_codes
    assert 500 in metadata.response_codes


def test_resource_from_endpoint():
    """Test resource type extraction from endpoint"""
    extractor = MetadataExtractor()

    # Test various endpoint formats
    assert extractor._extract_resource_from_endpoint("/v1/roles") == "roles"
    assert extractor._extract_resource_from_endpoint("/api/v2/users/{id}") == "users"
    assert extractor._extract_resource_from_endpoint("/v1/clusters") == "clusters"


def test_no_false_positive_endpoints():
    """Test that we don't extract false positive endpoints"""
    extractor = MetadataExtractor()

    text = "Files are stored in /usr/local/bin directory."
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    # Should not extract /usr/local/bin as an API endpoint
    assert metadata.endpoint_path is None


def test_multiple_keywords_extracted():
    """Test that multiple important keywords are extracted"""
    extractor = MetadataExtractor()

    text = 'VMware Cloud Foundation supports "ADMIN", "OPERATOR", and "VIEWER" roles for vSphere management.'
    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    # Should extract quoted strings and capitalized terms
    assert len(metadata.keywords) > 0
    # Check for some expected keywords
    keywords_str = " ".join(metadata.keywords)
    assert "ADMIN" in keywords_str or "OPERATOR" in keywords_str or "VIEWER" in keywords_str


def test_empty_text_handling():
    """Test handling of empty or whitespace-only text"""
    extractor = MetadataExtractor()

    metadata = extractor.extract_metadata("   \n\n   ", "test.md", 0, {})

    # Should not crash, should return default metadata
    assert metadata.content_type == ContentType.DESCRIPTION
    assert metadata.keywords == []


def test_complex_json_with_nested_objects():
    """Test detection of complex nested JSON"""
    extractor = MetadataExtractor()

    text = """
    Sample response:
    {
      "elements": [
        {
          "id": "abc-123",
          "name": "ADMIN",
          "permissions": {
            "read": true,
            "write": true
          }
        }
      ]
    }
    """

    metadata = extractor.extract_metadata(text, "test.md", 0, {})

    assert metadata.has_json_example is True
    assert metadata.content_type == ContentType.EXAMPLE_JSON
    assert "ADMIN" in metadata.keywords


def test_heading_stack_empty_when_no_context():
    """Test that heading hierarchy is empty when no context provided"""
    extractor = MetadataExtractor()

    metadata = extractor.extract_metadata("Some text", "test.md", 0, {})

    assert metadata.heading_hierarchy == []
    assert metadata.chapter is None
    assert metadata.section is None
