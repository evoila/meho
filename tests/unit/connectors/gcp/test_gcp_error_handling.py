# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for GCP error parsing — KeyError defense-in-depth.

Verifies that _parse_gcp_error produces actionable INVALID_PARAMETERS
messages for KeyError exceptions, and that execute() short-circuits
on invalid parameters before the handler is reached.
"""

import pytest

from meho_app.modules.connectors.gcp.connector import GCPConnector


def _make_connector() -> GCPConnector:
    return GCPConnector(
        connector_id="test-err",
        config={"project_id": "my-project"},
        credentials={},
    )


class TestParseGcpErrorKeyError:
    """_parse_gcp_error must handle KeyError clearly."""

    def test_keyerror_produces_invalid_parameters(self) -> None:
        connector = _make_connector()
        result = connector._parse_gcp_error(KeyError("instance_name"), "get_instance")

        assert result["code"] == "INVALID_PARAMETERS"
        assert "instance_name" in result["message"]
        assert "missing required parameter" in result["message"].lower()
        assert result["details"]["missing_key"] == "instance_name"

    def test_keyerror_empty_args(self) -> None:
        connector = _make_connector()
        result = connector._parse_gcp_error(KeyError(), "get_instance")

        assert result["code"] == "INVALID_PARAMETERS"
        assert "unknown" in result["message"]

    def test_regular_exception_still_unknown(self) -> None:
        connector = _make_connector()
        result = connector._parse_gcp_error(RuntimeError("boom"), "get_instance")

        assert result["code"] == "UNKNOWN"
        assert "boom" in result["message"]


class TestExecuteValidationIntegration:
    """execute() validates params and returns clear errors."""

    @pytest.mark.asyncio
    async def test_wrong_param_name_rejected_before_handler(self) -> None:
        connector = _make_connector()
        connector._is_connected = True

        result = await connector.execute(
            "get_instance", {"name": "webapps-vm", "zone": "europe-west6-a"}
        )

        assert result.success is False
        assert result.error_code == "INVALID_PARAMETERS"
        assert "instance_name" in result.error
        assert "did you mean" in result.error.lower()

    @pytest.mark.asyncio
    async def test_correct_params_pass_validation(self) -> None:
        """Correct params pass validation; handler may still fail (no real GCP)
        but the error will NOT be INVALID_PARAMETERS."""
        connector = _make_connector()
        connector._is_connected = True

        result = await connector.execute(
            "get_instance",
            {"instance_name": "webapps-vm", "zone": "europe-west6-a"},
        )

        if not result.success:
            assert result.error_code != "INVALID_PARAMETERS"
