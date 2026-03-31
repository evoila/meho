"""Tests for topology entity extraction framework.

Tests BaseEntityExtractor ABC, extractor registry, and run_extraction side-effect.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meho_claude.core.topology.models import (
    ExtractionResult,
    TopologyEntity,
    TopologyRelationship,
)


def _make_entity(
    name="nginx",
    entity_type="Pod",
    connector_type="kubernetes",
    connector_id="k8s-prod",
    connector_name="prod-cluster",
    canonical_id="default/nginx",
    description="Nginx web server pod",
    scope=None,
):
    """Helper to create a TopologyEntity with sensible defaults."""
    return TopologyEntity(
        name=name,
        entity_type=entity_type,
        connector_type=connector_type,
        connector_id=connector_id,
        connector_name=connector_name,
        canonical_id=canonical_id,
        description=description,
        scope=scope or {},
    )


def _make_extraction_result(entities=None, relationships=None):
    """Helper to create an ExtractionResult."""
    return ExtractionResult(
        entities=entities or [],
        relationships=relationships or [],
        source_connector="prod-cluster",
        source_operation="list-pods",
    )


class TestExtractorRegistry:
    """Tests for @register_extractor and get_extractor_class."""

    def test_register_and_get_extractor(self):
        """register_extractor + get_extractor_class round-trip should work."""
        from meho_claude.core.topology.extractor import (
            BaseEntityExtractor,
            get_extractor_class,
            register_extractor,
        )

        @register_extractor("kubernetes")
        class K8sExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                return _make_extraction_result()

        cls = get_extractor_class("kubernetes")
        assert cls is K8sExtractor

    def test_get_extractor_class_unregistered_returns_none(self):
        """get_extractor_class for unregistered type should return None."""
        from meho_claude.core.topology.extractor import get_extractor_class

        assert get_extractor_class("rest") is None

    def test_get_extractor_class_nonexistent_returns_none(self):
        """get_extractor_class for completely unknown type should return None."""
        from meho_claude.core.topology.extractor import get_extractor_class

        assert get_extractor_class("nonexistent") is None

    def test_register_multiple_extractors(self):
        """Should support registering extractors for different connector types."""
        from meho_claude.core.topology.extractor import (
            BaseEntityExtractor,
            get_extractor_class,
            register_extractor,
        )

        @register_extractor("kubernetes")
        class K8sExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                return _make_extraction_result()

        @register_extractor("vmware")
        class VMwareExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                return _make_extraction_result()

        assert get_extractor_class("kubernetes") is K8sExtractor
        assert get_extractor_class("vmware") is VMwareExtractor

    def test_extractor_abc_requires_extract_method(self):
        """Subclass without extract() should raise TypeError on instantiation."""
        from meho_claude.core.topology.extractor import BaseEntityExtractor

        class IncompleteExtractor(BaseEntityExtractor):
            pass

        with pytest.raises(TypeError):
            IncompleteExtractor()


class TestRunExtraction:
    """Tests for run_extraction side-effect function."""

    def test_with_registered_extractor_stores_entities(self, topology_db, tmp_state_dir):
        """run_extraction with registered extractor should store entities in topology.db."""
        from meho_claude.core.topology.extractor import (
            BaseEntityExtractor,
            register_extractor,
            run_extraction,
        )

        entities = [_make_entity()]

        @register_extractor("kubernetes")
        class K8sExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                return _make_extraction_result(entities=entities)

        with (
            patch("meho_claude.core.topology.extractor.embed_topology_entities"),
            patch.dict("sys.modules", {"meho_claude.core.topology.extractors": MagicMock()}),
        ):
            run_extraction(
                tmp_state_dir, "prod-cluster", "kubernetes", "list-pods", {"items": []}
            )

        # Verify entity was stored
        row = topology_db.execute("SELECT COUNT(*) as c FROM topology_entities").fetchone()
        assert row["c"] == 1

    def test_no_registered_extractor_returns_silently(self, topology_db, tmp_state_dir):
        """run_extraction with no registered extractor should return without error."""
        from meho_claude.core.topology.extractor import run_extraction

        # REST has no extractor -- should not raise
        run_extraction(tmp_state_dir, "my-api", "rest", "listUsers", {"users": []})

        # No entities should be stored
        row = topology_db.execute("SELECT COUNT(*) as c FROM topology_entities").fetchone()
        assert row["c"] == 0

    def test_catches_exceptions_and_does_not_raise(self, topology_db, tmp_state_dir):
        """run_extraction should catch all exceptions and never raise."""
        from meho_claude.core.topology.extractor import (
            BaseEntityExtractor,
            register_extractor,
            run_extraction,
        )

        @register_extractor("kubernetes")
        class BrokenExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                raise RuntimeError("Extraction exploded!")

        # Should NOT raise
        run_extraction(tmp_state_dir, "prod", "kubernetes", "list-pods", {})

    def test_empty_extraction_result_skips_store(self, topology_db, tmp_state_dir):
        """run_extraction with empty result should skip store and embed."""
        from meho_claude.core.topology.extractor import (
            BaseEntityExtractor,
            register_extractor,
            run_extraction,
        )

        @register_extractor("kubernetes")
        class EmptyExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                return _make_extraction_result()  # Empty entities and relationships

        with patch("meho_claude.core.topology.extractor.embed_topology_entities") as mock_embed:
            run_extraction(tmp_state_dir, "prod", "kubernetes", "list-pods", {})

        # embed should NOT have been called
        mock_embed.assert_not_called()

    def test_embeds_entities_needing_embedding(self, topology_db, tmp_state_dir):
        """run_extraction should call embed_topology_entities for new/changed entities."""
        from meho_claude.core.topology.extractor import (
            BaseEntityExtractor,
            register_extractor,
            run_extraction,
        )

        entities = [_make_entity()]

        @register_extractor("kubernetes")
        class K8sExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                return _make_extraction_result(entities=entities)

        with patch("meho_claude.core.topology.extractor.embed_topology_entities") as mock_embed:
            run_extraction(tmp_state_dir, "prod", "kubernetes", "list-pods", {})

        mock_embed.assert_called_once()
        # Should have been called with state_dir and the entities needing embedding
        call_args = mock_embed.call_args
        assert call_args[0][0] == tmp_state_dir
        assert len(call_args[0][1]) == 1  # One entity needing embedding

    def test_skips_embed_when_no_entities_need_it(self, topology_db, tmp_state_dir):
        """run_extraction should skip embed when ingest returns empty entities_needing_embedding."""
        from meho_claude.core.topology.extractor import (
            BaseEntityExtractor,
            register_extractor,
            run_extraction,
        )

        entities = [_make_entity()]

        @register_extractor("kubernetes")
        class K8sExtractor(BaseEntityExtractor):
            def extract(self, connector_name, connector_type, operation_id, result_data):
                return _make_extraction_result(entities=entities)

        # First run stores entity (needs embedding)
        with patch("meho_claude.core.topology.extractor.embed_topology_entities"):
            run_extraction(tmp_state_dir, "prod", "kubernetes", "list-pods", {})

        # Second run with same entity (no change -- should NOT call embed)
        with patch("meho_claude.core.topology.extractor.embed_topology_entities") as mock_embed:
            run_extraction(tmp_state_dir, "prod", "kubernetes", "list-pods", {})

        mock_embed.assert_not_called()
