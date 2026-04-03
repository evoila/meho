# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Metadata extraction for knowledge chunks.

Extracts structured metadata from text to enable enhanced retrieval.
"""

# mypy: disable-error-code="no-any-return,var-annotated"
import re
from typing import Any

from .schemas import ChunkMetadata, ContentType


class MetadataExtractor:
    """Extract structured metadata from text chunks"""

    # Common API resource types
    RESOURCE_TYPES = {  # noqa: RUF012 -- mutable default is intentional class state
        "roles",
        "users",
        "domains",
        "clusters",
        "hosts",
        "networks",
        "datastores",
        "vms",
        "workloads",
        "backups",
        "licenses",
        "certificates",
        "credentials",
        "groups",
        "permissions",
    }

    # HTTP methods
    HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}  # noqa: RUF012 -- mutable default is intentional class state

    def extract_metadata(
        self, text: str, document_name: str, _chunk_index: int, document_context: dict[str, Any]
    ) -> ChunkMetadata:
        """
        Extract all metadata from a text chunk.

        Args:
            text: The chunk text
            document_name: Source document filename
            chunk_index: Position in document
            document_context: Context from document processor (headings, etc.)

        Returns:
            ChunkMetadata with all extracted information
        """
        metadata = ChunkMetadata()

        # Extract structural metadata
        metadata.chapter = self._extract_chapter(document_context)
        metadata.section = self._extract_section(document_context)
        metadata.heading_hierarchy = document_context.get("heading_stack", [])

        # Detect content type
        metadata.content_type = self._detect_content_type(text)
        metadata.has_code_example = self._has_code(text)
        metadata.has_json_example = self._has_json(text)
        metadata.has_table = self._has_table(text)

        # Extract API-specific metadata
        if endpoint := self._extract_endpoint_path(text):
            metadata.endpoint_path = endpoint
            metadata.resource_type = self._extract_resource_from_endpoint(endpoint)

        metadata.http_method = self._extract_http_method(text)

        # Extract keywords and entities
        metadata.keywords = self._extract_keywords(text)
        metadata.entity_names = self._extract_entities(text)

        # Technical details
        metadata.programming_language = self._detect_language(text)
        metadata.response_codes = self._extract_response_codes(text)

        return metadata

    def _detect_content_type(self, text: str) -> ContentType:
        """Classify the type of content in this chunk"""
        text_lower = text.lower()

        # Check for JSON examples
        if self._has_json(text):  # noqa: SIM102 -- readability preferred over collapse
            if any(word in text_lower for word in ["example", "sample", "response"]):
                return ContentType.EXAMPLE_JSON

        # Check for code snippets
        if self._has_code(text) and not self._has_json(text):
            return ContentType.EXAMPLE_CODE

        # Check for schemas
        if "schema" in text_lower or "type:" in text_lower:  # noqa: SIM102 -- readability preferred over collapse
            if self._has_structured_format(text):
                return ContentType.SCHEMA

        # Check for parameters
        if any(word in text_lower for word in ["parameter", "query param", "path param"]):  # noqa: SIM102 -- readability preferred over collapse
            if self._has_list_format(text):
                return ContentType.PARAMETERS

        # Check for tables
        if self._has_table(text):
            return ContentType.TABLE

        # Check for lists
        if self._has_list_format(text):
            return ContentType.LIST

        # Check for overviews
        if any(word in text_lower for word in ["overview", "introduction", "about"]):
            return ContentType.OVERVIEW

        # Default to description
        return ContentType.DESCRIPTION

    def _extract_endpoint_path(self, text: str) -> str | None:
        """Extract API endpoint path like /v1/roles"""
        # Pattern: /path/to/endpoint with version numbers, hyphens, etc.
        pattern = r"(/(?:api/)?v?\d+(?:/[\w\-{}]+)+)"
        matches = re.findall(pattern, text, re.IGNORECASE)

        if matches:
            # Return the longest match (most specific)
            return max(matches, key=len)

        # Also check for paths without version
        pattern = r"(/(?:api/)?[\w\-]+(?:/[\w\-{}]+)+)"
        matches = re.findall(pattern, text, re.IGNORECASE)

        if matches:
            # Filter out common false positives
            valid_paths = [
                m for m in matches if not m.startswith("/usr") and not m.startswith("/etc")
            ]
            if valid_paths:
                return max(valid_paths, key=len)

        return None

    def _extract_resource_from_endpoint(self, endpoint: str) -> str | None:
        """Extract resource type from endpoint path"""
        # Split path and check each segment
        parts = endpoint.strip("/").split("/")

        # Check from end to start (most specific first)
        for part in reversed(parts):
            # Remove common patterns
            cleaned = part.lower().replace("-", "").replace("_", "")
            cleaned = re.sub(r"\{.*?\}", "", cleaned)  # Remove {id} placeholders

            # Check against known resource types
            for resource in self.RESOURCE_TYPES:
                if resource in cleaned:
                    return resource

        return None

    def _extract_http_method(self, text: str) -> str | None:
        """Extract HTTP method from text"""
        # Look for methods in context
        pattern = r"\b(GET|POST|PUT|DELETE|PATCH)\b"
        matches = re.findall(pattern, text, re.IGNORECASE)

        if matches:
            return matches[0].upper()

        return None

    def _has_json(self, text: str) -> bool:
        """Check if text contains JSON"""
        # Look for JSON structure indicators
        json_indicators = [
            r'\{[\s\n]*"[\w_]+"[\s\n]*:',  # Object start
            r"\[[\s\n]*\{",  # Array of objects
            r'"elements"[\s\n]*:[\s\n]*\[',  # Common API pattern
        ]

        return any(re.search(pattern, text) for pattern in json_indicators)

    def _has_code(self, text: str) -> bool:
        """Check if text contains code"""
        code_indicators = [
            "```",  # Markdown code block
            "    ",  # Indented code (4 spaces)
            "\t",  # Tab-indented code
            "curl ",  # Command line
            "import ",  # Python import
            "function ",  # JavaScript
        ]

        return any(indicator in text for indicator in code_indicators)

    def _has_table(self, text: str) -> bool:
        """Check if text contains a table"""
        # Markdown tables or aligned columns
        lines = text.split("\n")

        # Check for markdown tables
        if any("|" in line and line.count("|") >= 2 for line in lines):
            return True

        # Check for aligned columns (multiple spaces)
        return bool(any(re.search(r"\w+\s{3,}\w+", line) for line in lines))

    def _has_list_format(self, text: str) -> bool:
        """Check if text is primarily a list"""
        lines = [l.strip() for l in text.split("\n") if l.strip()]  # noqa: E741 -- domain-specific variable name
        if not lines:
            return False

        # Check for bullet points or numbered lists
        list_indicators = [
            r"^\d+\.",  # 1. 2. 3.
            r"^[\-\*\+]",  # - * +
            r"^\w+:",  # name: description
        ]

        list_lines = sum(
            1 for line in lines if any(re.match(pattern, line) for pattern in list_indicators)
        )

        # If >50% of lines are list items, it's a list
        return list_lines / len(lines) > 0.5

    def _has_structured_format(self, text: str) -> bool:
        """Check if text has structured format (schema-like)"""
        # Look for key-value patterns
        pattern = r"\w+:\s*\w+"
        matches = re.findall(pattern, text)
        return len(matches) > 3

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract important keywords from text"""
        keywords = set()

        # Extract quoted strings
        quoted = re.findall(r'"([^"]{2,})"', text)
        keywords.update(quoted)

        # Extract UPPERCASE terms (likely constants or important names)
        uppercase = re.findall(r"\b[A-Z]{2,}\b", text)
        # Filter out common words
        common_words = {
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "PATCH",
            "HTTP",
            "API",
            "REST",
            "JSON",
            "XML",
            "ID",
        }
        keywords.update(term for term in uppercase if term not in common_words)

        # Extract capitalized words (likely proper nouns)
        capitalized = re.findall(r"\b[A-Z][a-z]+\b", text)
        # Only keep if they appear multiple times or are longer
        keyword_counts = {}
        for word in capitalized:
            keyword_counts[word] = keyword_counts.get(word, 0) + 1
        keywords.update(
            word for word, count in keyword_counts.items() if count > 1 or len(word) > 8
        )

        return list(keywords)[:20]  # Limit to top 20

    def _extract_entities(self, text: str) -> list[str]:
        """Extract named entities (simplified version)"""
        # This is a simplified version. For production, consider using spaCy
        entities = set()

        # Extract version numbers
        versions = re.findall(r"v\d+(?:\.\d+)*", text, re.IGNORECASE)
        entities.update(versions)

        return list(entities)[:10]

    def _extract_chapter(self, context: dict[str, Any]) -> str | None:
        """Extract chapter name from context"""
        heading_stack = context.get("heading_stack", [])
        if heading_stack:
            # Return top-level heading
            return heading_stack[0] if len(heading_stack) > 0 else None
        return None

    def _extract_section(self, context: dict[str, Any]) -> str | None:
        """Extract section name from context"""
        heading_stack = context.get("heading_stack", [])
        if len(heading_stack) > 1:
            # Return second-level heading
            return heading_stack[1]
        return None

    def _detect_language(self, text: str) -> str | None:
        """Detect programming language of code"""
        if not self._has_code(text):
            return None

        # Check for language indicators
        if self._has_json(text):
            return "json"
        if "def " in text or "import " in text:
            return "python"
        if "function " in text or "const " in text or "let " in text:
            return "javascript"
        if "curl " in text:
            return "bash"

        return "unknown"

    def _extract_response_codes(self, text: str) -> list[int]:
        """Extract HTTP response codes mentioned"""
        # Look for patterns like "200 OK", "404", "500 Internal Server Error"
        pattern = r"\b([2345]\d{2})\b"
        matches = re.findall(pattern, text)
        return [int(code) for code in set(matches)]
