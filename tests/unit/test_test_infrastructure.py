# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Test that the test infrastructure itself works correctly.
"""

import pytest


@pytest.mark.unit
def test_pytest_runs():
    """Verify pytest is working"""
    assert True


@pytest.mark.unit
def test_test_markers_work():
    """Verify test markers are configured"""
    # This test should be found when running: pytest -m unit
    assert True


@pytest.mark.unit
async def test_async_tests_work():
    """Verify async tests work with pytest-asyncio"""
    import asyncio

    await asyncio.sleep(0.001)
    assert True


@pytest.mark.unit
def test_conftest_loaded():
    """Verify conftest.py is loaded"""
    # If this runs, conftest.py was loaded successfully
    assert True


@pytest.mark.unit
def test_unit_conftest_loaded(mock_meho_dependencies):
    """Test that unit conftest.py fixtures are available"""
    assert mock_meho_dependencies is not None
    assert mock_meho_dependencies.user_context.tenant_id == "test-tenant"


@pytest.mark.unit
def test_llm_guard_active():
    """Test that ALLOW_MODEL_REQUESTS=False is set by root conftest"""
    import pydantic_ai.models

    assert not pydantic_ai.models.ALLOW_MODEL_REQUESTS


@pytest.mark.unit
def test_test_environment_variables():
    """Test that test environment is configured"""
    import os

    # Should be set in conftest.py
    assert os.environ.get("ENV") == "test"
