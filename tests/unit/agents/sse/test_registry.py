# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for EventRegistry class.

These tests verify:
1. EventRegistry.list_all() returns all event types
2. EventRegistry.get_schema() returns correct schemas
3. EventRegistry.generate_markdown() produces valid documentation
4. All event types have proper schemas
"""

from __future__ import annotations

from meho_app.modules.agents.sse import EventRegistry, EventSchema
from meho_app.modules.agents.sse.registry import EVENT_SCHEMAS


class TestEventRegistryBasic:
    """Tests for basic EventRegistry functionality."""

    def test_registry_importable_from_sse(self) -> None:
        """EventRegistry should be importable from sse module."""
        from meho_app.modules.agents.sse import EventRegistry as ImportedRegistry

        assert ImportedRegistry is EventRegistry

    def test_schema_importable_from_sse(self) -> None:
        """EventSchema should be importable from sse module."""
        from meho_app.modules.agents.sse import EventSchema as ImportedSchema

        assert ImportedSchema is EventSchema


class TestEventRegistryListAll:
    """Tests for list_all method."""

    def test_list_all_returns_list(self) -> None:
        """list_all should return a list of strings."""
        events = EventRegistry.list_all()
        assert isinstance(events, list)
        assert all(isinstance(e, str) for e in events)

    def test_list_all_contains_core_events(self) -> None:
        """list_all should contain all core event types."""
        events = EventRegistry.list_all()
        core_events = [
            "agent_start",
            "agent_complete",
            "thought",
            "action",
            "observation",
            "final_answer",
            "error",
            "approval_required",
            "tool_start",
            "tool_complete",
            "node_enter",
            "node_exit",
        ]
        for event in core_events:
            assert event in events, f"Missing event: {event}"

    def test_list_all_is_sorted(self) -> None:
        """list_all should return events in alphabetical order."""
        events = EventRegistry.list_all()
        assert events == sorted(events)


class TestEventRegistryGetSchema:
    """Tests for get_schema method."""

    def test_get_schema_returns_schema(self) -> None:
        """get_schema should return EventSchema for valid type."""
        schema = EventRegistry.get_schema("thought")
        assert schema is not None
        assert isinstance(schema, EventSchema)
        assert schema.name == "thought"

    def test_get_schema_returns_none_for_invalid(self) -> None:
        """get_schema should return None for invalid type."""
        schema = EventRegistry.get_schema("nonexistent_event")
        assert schema is None

    def test_get_schema_has_all_fields(self) -> None:
        """Each schema should have all required fields."""
        for event_type in EventRegistry.list_all():
            schema = EventRegistry.get_schema(event_type)
            assert schema is not None
            assert schema.name == event_type
            assert isinstance(schema.description, str)
            assert len(schema.description) > 0
            assert isinstance(schema.data_fields, dict)
            assert isinstance(schema.example, dict)


class TestEventRegistryGetAllSchemas:
    """Tests for get_all_schemas method."""

    def test_get_all_schemas_returns_dict(self) -> None:
        """get_all_schemas should return a dictionary."""
        schemas = EventRegistry.get_all_schemas()
        assert isinstance(schemas, dict)

    def test_get_all_schemas_matches_event_schemas(self) -> None:
        """get_all_schemas should return copy of EVENT_SCHEMAS."""
        schemas = EventRegistry.get_all_schemas()
        assert schemas == EVENT_SCHEMAS

    def test_get_all_schemas_is_copy(self) -> None:
        """get_all_schemas should return a copy, not the original."""
        schemas = EventRegistry.get_all_schemas()
        # Modifying should not affect original
        schemas["test"] = EventSchema(
            name="test",
            description="test",
            data_fields={},
            example={},
        )
        assert "test" not in EVENT_SCHEMAS


class TestEventRegistryValidation:
    """Tests for validate_event_type method."""

    def test_validate_valid_event(self) -> None:
        """validate_event_type should return True for valid events."""
        assert EventRegistry.validate_event_type("thought") is True
        assert EventRegistry.validate_event_type("action") is True
        assert EventRegistry.validate_event_type("final_answer") is True

    def test_validate_invalid_event(self) -> None:
        """validate_event_type should return False for invalid events."""
        assert EventRegistry.validate_event_type("nonexistent") is False
        assert EventRegistry.validate_event_type("") is False


class TestEventRegistryMarkdown:
    """Tests for generate_markdown method."""

    def test_generate_markdown_returns_string(self) -> None:
        """generate_markdown should return a string."""
        md = EventRegistry.generate_markdown()
        assert isinstance(md, str)

    def test_generate_markdown_has_title(self) -> None:
        """generate_markdown should include a title."""
        md = EventRegistry.generate_markdown()
        assert "# SSE Event Reference" in md

    def test_generate_markdown_has_all_events(self) -> None:
        """generate_markdown should document all events."""
        md = EventRegistry.generate_markdown()
        for event_type in EventRegistry.list_all():
            assert f"## {event_type}" in md

    def test_generate_markdown_has_examples(self) -> None:
        """generate_markdown should include examples."""
        md = EventRegistry.generate_markdown()
        assert "### Example" in md
        assert "```json" in md

    def test_generate_markdown_has_data_fields(self) -> None:
        """generate_markdown should document data fields."""
        md = EventRegistry.generate_markdown()
        assert "### Data Fields" in md


class TestEventSchemaDataclass:
    """Tests for EventSchema dataclass."""

    def test_create_event_schema(self) -> None:
        """EventSchema should be creatable with all fields."""
        schema = EventSchema(
            name="test_event",
            description="A test event",
            data_fields={"field1": "Description 1"},
            example={"field1": "value1"},
        )
        assert schema.name == "test_event"
        assert schema.description == "A test event"
        assert schema.data_fields == {"field1": "Description 1"}
        assert schema.example == {"field1": "value1"}


class TestEventSchemaCompleteness:
    """Tests to ensure all event types have complete documentation."""

    def test_all_events_have_descriptions(self) -> None:
        """All events should have non-empty descriptions."""
        for name, schema in EVENT_SCHEMAS.items():
            assert schema.description, f"{name} has empty description"
            assert len(schema.description) > 10, f"{name} has too short description"

    def test_all_events_have_data_fields(self) -> None:
        """All events should document their data fields."""
        for name, schema in EVENT_SCHEMAS.items():
            # Some events might have empty data, but should still have dict
            assert isinstance(schema.data_fields, dict), f"{name} has invalid data_fields"

    def test_all_events_have_examples(self) -> None:
        """All events should have example data."""
        for name, schema in EVENT_SCHEMAS.items():
            assert isinstance(schema.example, dict), f"{name} has invalid example"

    def test_example_fields_match_data_fields(self) -> None:
        """Example fields should match documented data fields."""
        for name, schema in EVENT_SCHEMAS.items():
            example_keys = set(schema.example.keys())
            doc_keys = set(schema.data_fields.keys())
            # All example keys should be documented
            undocumented = example_keys - doc_keys
            assert not undocumented, f"{name} has undocumented example fields: {undocumented}"
