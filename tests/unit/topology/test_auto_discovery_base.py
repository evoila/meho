# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for topology auto-discovery base classes.

Tests ExtractedEntity, ExtractedRelationship, and BaseExtractor.
"""

import json

import pytest

from meho_app.modules.topology.auto_discovery.base import (
    BaseExtractor,
    ExtractedEntity,
    ExtractedRelationship,
)


class TestExtractedEntity:
    """Tests for ExtractedEntity dataclass."""

    def test_create_minimal(self):
        """Test creating entity with minimal required fields."""
        entity = ExtractedEntity(
            name="web-01",
            description="A web server",
            connector_id="abc123",
        )

        assert entity.name == "web-01"
        assert entity.description == "A web server"
        assert entity.connector_id == "abc123"
        assert entity.connector_name is None
        assert entity.raw_attributes == {}

    def test_create_full(self):
        """Test creating entity with all fields."""
        entity = ExtractedEntity(
            name="web-01",
            description="VMware VM web-01, 4 vCPU, 8192MB RAM",
            connector_id="abc123",
            connector_name="Production vCenter",
            raw_attributes={"power_state": "poweredOn", "ip": "192.168.1.10"},
        )

        assert entity.name == "web-01"
        assert entity.description == "VMware VM web-01, 4 vCPU, 8192MB RAM"
        assert entity.connector_id == "abc123"
        assert entity.connector_name == "Production vCenter"
        assert entity.raw_attributes["power_state"] == "poweredOn"
        assert entity.raw_attributes["ip"] == "192.168.1.10"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        entity = ExtractedEntity(
            name="web-01",
            description="A web server",
            connector_id="abc123",
            connector_name="vCenter",
            raw_attributes={"cpu": 4},
        )

        data = entity.to_dict()

        assert data["name"] == "web-01"
        assert data["description"] == "A web server"
        assert data["connector_id"] == "abc123"
        assert data["connector_name"] == "vCenter"
        assert data["raw_attributes"] == {"cpu": 4}

    def test_to_json(self):
        """Test serialization to JSON."""
        entity = ExtractedEntity(
            name="web-01",
            description="A web server",
            connector_id="abc123",
        )

        json_str = entity.to_json()
        data = json.loads(json_str)

        assert data["name"] == "web-01"
        assert data["description"] == "A web server"

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "name": "web-01",
            "description": "A web server",
            "connector_id": "abc123",
            "connector_name": "vCenter",
            "raw_attributes": {"cpu": 4},
        }

        entity = ExtractedEntity.from_dict(data)

        assert entity.name == "web-01"
        assert entity.description == "A web server"
        assert entity.connector_id == "abc123"
        assert entity.connector_name == "vCenter"
        assert entity.raw_attributes == {"cpu": 4}

    def test_from_dict_minimal(self):
        """Test deserialization with minimal fields."""
        data = {
            "name": "web-01",
            "description": "A web server",
            "connector_id": "abc123",
        }

        entity = ExtractedEntity.from_dict(data)

        assert entity.name == "web-01"
        assert entity.connector_name is None
        assert entity.raw_attributes == {}

    def test_from_json(self):
        """Test deserialization from JSON."""
        json_str = '{"name": "web-01", "description": "A server", "connector_id": "abc"}'

        entity = ExtractedEntity.from_json(json_str)

        assert entity.name == "web-01"
        assert entity.description == "A server"
        assert entity.connector_id == "abc"

    def test_roundtrip(self):
        """Test serialization/deserialization roundtrip."""
        original = ExtractedEntity(
            name="web-01",
            description="VMware VM",
            connector_id="abc123",
            connector_name="vCenter",
            raw_attributes={"nested": {"value": 123}},
        )

        json_str = original.to_json()
        restored = ExtractedEntity.from_json(json_str)

        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.connector_id == original.connector_id
        assert restored.connector_name == original.connector_name
        assert restored.raw_attributes == original.raw_attributes


class TestExtractedRelationship:
    """Tests for ExtractedRelationship dataclass."""

    def test_create(self):
        """Test creating a relationship."""
        rel = ExtractedRelationship(
            from_entity_name="web-01",
            to_entity_name="esxi-01",
            relationship_type="runs_on",
        )

        assert rel.from_entity_name == "web-01"
        assert rel.to_entity_name == "esxi-01"
        assert rel.relationship_type == "runs_on"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        rel = ExtractedRelationship(
            from_entity_name="web-01",
            to_entity_name="esxi-01",
            relationship_type="runs_on",
        )

        data = rel.to_dict()

        assert data["from_entity_name"] == "web-01"
        assert data["to_entity_name"] == "esxi-01"
        assert data["relationship_type"] == "runs_on"

    def test_to_json(self):
        """Test serialization to JSON."""
        rel = ExtractedRelationship(
            from_entity_name="web-01",
            to_entity_name="esxi-01",
            relationship_type="runs_on",
        )

        json_str = rel.to_json()
        data = json.loads(json_str)

        assert data["from_entity_name"] == "web-01"
        assert data["to_entity_name"] == "esxi-01"

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "from_entity_name": "web-01",
            "to_entity_name": "esxi-01",
            "relationship_type": "runs_on",
        }

        rel = ExtractedRelationship.from_dict(data)

        assert rel.from_entity_name == "web-01"
        assert rel.to_entity_name == "esxi-01"
        assert rel.relationship_type == "runs_on"

    def test_from_json(self):
        """Test deserialization from JSON."""
        json_str = (
            '{"from_entity_name": "vm", "to_entity_name": "host", "relationship_type": "runs_on"}'
        )

        rel = ExtractedRelationship.from_json(json_str)

        assert rel.from_entity_name == "vm"
        assert rel.to_entity_name == "host"
        assert rel.relationship_type == "runs_on"

    def test_roundtrip(self):
        """Test serialization/deserialization roundtrip."""
        original = ExtractedRelationship(
            from_entity_name="web-01",
            to_entity_name="esxi-01",
            relationship_type="runs_on",
        )

        json_str = original.to_json()
        restored = ExtractedRelationship.from_json(json_str)

        assert restored.from_entity_name == original.from_entity_name
        assert restored.to_entity_name == original.to_entity_name
        assert restored.relationship_type == original.relationship_type


class TestBaseExtractor:
    """Tests for BaseExtractor abstract base class."""

    def test_is_abstract(self):
        """Test that BaseExtractor cannot be instantiated directly."""
        with pytest.raises(TypeError):
            BaseExtractor()

    def test_concrete_implementation(self):
        """Test that a concrete implementation works."""

        class ConcreteExtractor(BaseExtractor):
            def can_extract(self, operation_id: str) -> bool:
                return operation_id == "test_op"

            def extract(self, operation_id, result_data, connector_id, connector_name=None):
                if operation_id == "test_op":
                    entity = ExtractedEntity(
                        name="test",
                        description="test entity",
                        connector_id=connector_id,
                    )
                    return [entity], []
                return [], []

        extractor = ConcreteExtractor()

        assert extractor.can_extract("test_op") is True
        assert extractor.can_extract("other_op") is False

        entities, rels = extractor.extract("test_op", {}, "abc123")
        assert len(entities) == 1
        assert entities[0].name == "test"
        assert len(rels) == 0

    def test_get_supported_operations_default(self):
        """Test that get_supported_operations returns empty list by default."""

        class ConcreteExtractor(BaseExtractor):
            def can_extract(self, operation_id: str) -> bool:
                return False

            def extract(self, operation_id, result_data, connector_id, connector_name=None):
                return [], []

        extractor = ConcreteExtractor()
        assert extractor.get_supported_operations() == []

    def test_get_supported_operations_override(self):
        """Test overriding get_supported_operations."""

        class ConcreteExtractor(BaseExtractor):
            def can_extract(self, operation_id: str) -> bool:
                return operation_id in ["op1", "op2"]

            def extract(self, operation_id, result_data, connector_id, connector_name=None):
                return [], []

            def get_supported_operations(self):
                return ["op1", "op2"]

        extractor = ConcreteExtractor()
        assert extractor.get_supported_operations() == ["op1", "op2"]
