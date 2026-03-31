# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test PydanticAI message serialization to JSON.

This test verifies that we can properly serialize PydanticAI messages containing
all types of message parts (SystemPromptPart, UserPromptPart, ToolCallPart, etc.)
to JSON and store them in PostgreSQL.

This fixes the bug: "Object of type SystemPromptPart is not JSON serializable"
"""

import json
from dataclasses import asdict
from datetime import datetime

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from meho_app.modules.agents.message_serialization import (
    serialize_message_list,
    serialize_pydanticai_message,
    serialize_value,
    validate_json_serializable,
)


class TestPydanticAIMessageSerialization:
    """Test that all PydanticAI message types can be serialized to JSON"""

    def test_system_prompt_part_serialization(self):
        """Test SystemPromptPart can be serialized"""
        message = ModelRequest(parts=[SystemPromptPart(content="You are a helpful assistant")])

        # Use custom serializer to convert dataclass to JSON-compatible dict
        serialized = serialize_pydanticai_message(message)

        # Should be JSON-serializable
        json_str = json.dumps(serialized)
        assert json_str is not None

        # Should contain the system prompt
        loaded = json.loads(json_str)
        assert "parts" in loaded
        assert len(loaded["parts"]) == 1
        assert loaded["parts"][0]["part_kind"] == "system-prompt"
        assert "assistant" in loaded["parts"][0]["content"].lower()

    def test_user_prompt_part_serialization(self):
        """Test UserPromptPart can be serialized"""
        message = ModelRequest(parts=[UserPromptPart(content="What is the weather?")])

        serialized = serialize_pydanticai_message(message)
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        assert loaded["parts"][0]["part_kind"] == "user-prompt"
        assert "weather" in loaded["parts"][0]["content"]

    def test_text_part_serialization(self):
        """Test TextPart can be serialized"""
        message = ModelResponse(parts=[TextPart(content="The weather is sunny")])

        serialized = serialize_pydanticai_message(message)
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        assert loaded["parts"][0]["part_kind"] == "text"
        assert "sunny" in loaded["parts"][0]["content"]

    def test_tool_call_part_serialization(self):
        """Test ToolCallPart can be serialized"""
        message = ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="search_docs", args={"query": "kubernetes"}, tool_call_id="call_123"
                )
            ]
        )

        serialized = serialize_pydanticai_message(message)
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        assert loaded["parts"][0]["part_kind"] == "tool-call"
        assert loaded["parts"][0]["tool_name"] == "search_docs"
        assert loaded["parts"][0]["args"]["query"] == "kubernetes"

    def test_tool_return_part_serialization(self):
        """Test ToolReturnPart can be serialized"""
        message = ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search_docs", content="Documentation found", tool_call_id="call_123"
                )
            ]
        )

        serialized = serialize_pydanticai_message(message)
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        assert loaded["parts"][0]["part_kind"] == "tool-return"
        assert loaded["parts"][0]["tool_name"] == "search_docs"
        assert "Documentation" in loaded["parts"][0]["content"]

    def test_mixed_message_parts_serialization(self):
        """Test message with multiple different part types"""
        # This simulates a real conversation with tool calls
        messages = [
            # User request
            ModelRequest(parts=[UserPromptPart(content="Search for kubernetes docs")]),
            # Assistant with tool call
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="search_docs", args={"query": "kubernetes"}, tool_call_id="call_1"
                    )
                ]
            ),
            # Tool result
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="search_docs", content="Found 5 documents", tool_call_id="call_1"
                    )
                ]
            ),
            # Assistant final response
            ModelResponse(parts=[TextPart(content="I found 5 documents about Kubernetes")]),
        ]

        # Serialize all messages using the list serializer
        serialized_messages = serialize_message_list(messages)

        # Should be JSON-serializable
        json_str = json.dumps(serialized_messages)
        loaded = json.loads(json_str)

        # Verify we have 4 messages
        assert len(loaded) == 4

        # Verify structure
        assert loaded[0]["parts"][0]["part_kind"] == "user-prompt"
        assert loaded[1]["parts"][0]["part_kind"] == "tool-call"
        assert loaded[2]["parts"][0]["part_kind"] == "tool-return"
        assert loaded[3]["parts"][0]["part_kind"] == "text"

    def test_conversation_with_system_prompt(self):
        """Test full conversation with system prompt"""
        messages = [
            ModelRequest(
                parts=[SystemPromptPart(content="You are MEHO"), UserPromptPart(content="Help me")]
            ),
            ModelResponse(parts=[TextPart(content="I can help you")]),
        ]

        # Serialize and verify
        serialized = serialize_message_list(messages)
        json_str = json.dumps(serialized)
        assert json_str is not None

        loaded = json.loads(json_str)
        assert len(loaded) == 2
        assert len(loaded[0]["parts"]) == 2  # System + User prompt

    def test_round_trip_serialization(self):
        """Test that we can serialize and deserialize back to dict"""
        original_message = ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="call_endpoint",
                    args={"endpoint_id": "ep-123", "path_params": {"id": "vm-1"}},
                    tool_call_id="call_abc",
                ),
                TextPart(content="Calling the endpoint"),
            ]
        )

        # Serialize to JSON-compatible dict
        serialized = serialize_pydanticai_message(original_message)

        # Convert to JSON string and back
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        # Verify structure is preserved
        assert len(loaded["parts"]) == 2
        assert loaded["parts"][0]["part_kind"] == "tool-call"
        assert loaded["parts"][0]["tool_name"] == "call_endpoint"
        assert loaded["parts"][0]["args"]["endpoint_id"] == "ep-123"
        assert loaded["parts"][1]["part_kind"] == "text"
        assert loaded["parts"][1]["content"] == "Calling the endpoint"

    def test_empty_parts_serialization(self):
        """Test message with no parts (edge case)"""
        message = ModelRequest(parts=[])

        serialized = serialize_pydanticai_message(message)
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        assert loaded["parts"] == []

    def test_postgres_jsonb_compatibility(self):
        """Test that serialized messages are compatible with PostgreSQL JSONB"""
        # PostgreSQL JSONB supports:
        # - strings, numbers, booleans, null
        # - arrays
        # - objects
        # But NOT: datetime objects, custom classes, etc.

        message = ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="batch_get_endpoint",
                    args={
                        "endpoint_id": "ep-123",
                        "parameter_sets": [
                            {"path_params": {"id": "res-1"}},
                            {"path_params": {"id": "res-2"}},
                        ],
                        "connector_id": "conn-456",
                    },
                    tool_call_id="call_xyz",
                )
            ]
        )

        serialized = serialize_pydanticai_message(message)

        # Should be JSON-serializable (PostgreSQL JSONB requirement)
        json_str = json.dumps(serialized)
        assert json_str is not None

        # Verify with validation function
        assert validate_json_serializable(serialized)


class TestMessageSerializationRegression:
    """Regression tests for the 'SystemPromptPart is not JSON serializable' bug"""

    def test_system_prompt_part_without_serializer_fails(self):
        """
        REGRESSION TEST: Without custom serializer, SystemPromptPart fails.

        This documents the bug we're fixing: calling asdict() on messages
        with datetime fields produces non-JSON-serializable dicts.
        """
        message = ModelRequest(parts=[SystemPromptPart(content="System prompt")])

        # Without custom serializer, asdict() produces datetime objects
        message_dict = asdict(message)

        # This WILL fail with: "Object of type datetime is not JSON serializable"
        with pytest.raises(TypeError, match="not JSON serializable"):
            json.dumps(message_dict)

    def test_system_prompt_part_with_serializer_succeeds(self):
        """
        FIX VERIFICATION: With custom serializer, SystemPromptPart works.

        This is the fix: serialize_pydanticai_message() recursively converts
        all nested dataclasses and datetime objects to JSON-compatible values.
        """
        message = ModelRequest(parts=[SystemPromptPart(content="System prompt")])

        # With custom serializer, we get JSON-compatible dicts
        serialized_correct = serialize_pydanticai_message(message)

        # This should succeed
        json_str = json.dumps(serialized_correct)
        assert json_str is not None

        # And we can load it back
        loaded = json.loads(json_str)
        assert loaded["parts"][0]["part_kind"] == "system-prompt"


class TestSerializationUtilities:
    """Test the serialization utility functions"""

    def test_serialize_value_datetime(self):
        """Test datetime serialization"""
        dt = datetime(2025, 1, 1, 12, 0, 0)  # noqa: DTZ001 -- naive datetime for test compatibility
        serialized = serialize_value(dt)
        assert isinstance(serialized, str)
        assert "2025-01-01" in serialized

    def test_serialize_value_nested_dict(self):
        """Test nested dict with datetime"""
        data = {
            "timestamp": datetime(2025, 1, 1),  # noqa: DTZ001 -- naive datetime for test compatibility
            "nested": {"value": 42, "date": datetime(2025, 2, 1)},  # noqa: DTZ001 -- naive datetime for test compatibility
        }
        serialized = serialize_value(data)

        # Should be JSON-serializable
        json_str = json.dumps(serialized)
        loaded = json.loads(json_str)

        assert isinstance(loaded["timestamp"], str)
        assert isinstance(loaded["nested"]["date"], str)
        assert loaded["nested"]["value"] == 42

    def test_validate_json_serializable_success(self):
        """Test validation of JSON-serializable data"""
        data = {"key": "value", "number": 42, "list": [1, 2, 3]}
        assert validate_json_serializable(data)

    def test_validate_json_serializable_failure(self):
        """Test validation of non-JSON-serializable data"""
        data = {"datetime": datetime(2025, 1, 1)}  # noqa: DTZ001 -- naive datetime for test compatibility
        assert not validate_json_serializable(data)

    def test_serialize_message_list_with_error(self):
        """Test that serialize_message_list handles errors gracefully"""

        # Create a mock object that will fail serialization
        class BadMessage:
            role = "test"

            def __dataclass_fields__(self):
                raise ValueError("Serialization error")

        messages = [BadMessage()]

        # Should not raise, but return fallback
        serialized = serialize_message_list(messages)

        assert len(serialized) == 1
        assert serialized[0]["role"] == "test"
        assert "_serialization_error" in serialized[0]
