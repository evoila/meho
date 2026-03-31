# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for agent API schemas.
"""

import pytest

from meho_app.modules.agents.api_schemas import HealthResponse


@pytest.mark.unit
def test_health_response():
    """Test health response"""
    resp = HealthResponse(status="healthy")

    assert resp.status == "healthy"
    assert resp.service == "meho-agent"
    assert resp.version == "0.1.0"
