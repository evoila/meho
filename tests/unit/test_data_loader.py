# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for fixture data loader.
"""

from pathlib import Path

import pytest

from tests.support.data_loader import FixtureLoader


@pytest.mark.unit
def test_fixture_loader_load_json():
    """Test loading JSON fixture file"""
    loader = FixtureLoader()

    data = loader.load_json("connectors")

    assert isinstance(data, list)
    assert len(data) > 0
    assert "id" in data[0]
    assert "name" in data[0]


@pytest.mark.unit
def test_fixture_loader_load_knowledge_chunks():
    """Test loading knowledge chunks fixture"""
    loader = FixtureLoader()

    data = loader.load_json("knowledge_chunks")

    assert isinstance(data, list)
    assert len(data) > 0
    assert all("text" in item for item in data)


@pytest.mark.unit
def test_fixture_loader_missing_file():
    """Test loading non-existent fixture raises error"""
    loader = FixtureLoader()

    with pytest.raises(FileNotFoundError):
        loader.load_json("nonexistent_fixture")


@pytest.mark.unit
def test_fixture_loader_custom_directory():
    """Test fixture loader with custom directory"""
    custom_dir = Path(__file__).parent.parent / "fixtures"
    loader = FixtureLoader(fixtures_dir=custom_dir)

    data = loader.load_json("connectors")
    assert isinstance(data, list)
