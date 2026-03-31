"""Tests for trust model enforcement."""

import pytest

from meho_claude.core.trust import enforce_trust


def _make_operation(trust_tier="READ", display_name="List Pods", connector_name="k8s-prod", description="List all pods"):
    return {
        "trust_tier": trust_tier,
        "display_name": display_name,
        "connector_name": connector_name,
        "description": description,
        "operation_id": "listPods",
    }


class TestEnforceTrust:
    def test_read_auto_executes(self):
        op = _make_operation(trust_tier="READ")
        result = enforce_trust(op, {"namespace": "default"})
        assert result is None  # None means allowed

    def test_write_requires_confirmation(self):
        op = _make_operation(trust_tier="WRITE", display_name="Create Deployment")
        result = enforce_trust(op, {"name": "my-deploy"})
        assert result is not None
        assert result["status"] == "confirmation_required"
        assert "Create Deployment" in result["operation"]
        assert "hint" in result

    def test_write_with_confirmed_passes(self):
        op = _make_operation(trust_tier="WRITE", display_name="Create Deployment")
        result = enforce_trust(op, {"name": "my-deploy"}, confirmed=True)
        assert result is None

    def test_destructive_requires_typed_confirmation(self):
        op = _make_operation(trust_tier="DESTRUCTIVE", display_name="Delete Pod")
        result = enforce_trust(op, {"name": "web-pod-1"})
        assert result is not None
        assert result["status"] == "destructive_confirmation"
        assert "confirm_text" in result
        assert "hint" in result

    def test_destructive_with_wrong_text_rejected(self):
        op = _make_operation(trust_tier="DESTRUCTIVE", display_name="Delete Pod")
        result = enforce_trust(op, {"name": "web-pod-1"}, confirm_text="wrong text")
        assert result is not None
        assert result["status"] == "destructive_confirmation"

    def test_destructive_with_matching_text_passes(self):
        op = _make_operation(trust_tier="DESTRUCTIVE", display_name="Delete Pod")
        params = {"name": "web-pod-1"}
        # Get the expected confirm text first
        result = enforce_trust(op, params)
        expected_text = result["confirm_text"]
        # Now confirm with the correct text
        result2 = enforce_trust(op, params, confirm_text=expected_text)
        assert result2 is None

    def test_confirmation_includes_connector_name(self):
        op = _make_operation(trust_tier="WRITE", connector_name="k8s-prod")
        result = enforce_trust(op, {})
        assert result["connector"] == "k8s-prod"

    def test_confirmation_includes_params(self):
        op = _make_operation(trust_tier="WRITE")
        params = {"namespace": "production"}
        result = enforce_trust(op, params)
        assert result["params"] == params

    def test_destructive_confirm_text_contains_operation_info(self):
        op = _make_operation(trust_tier="DESTRUCTIVE", display_name="Delete Pod")
        result = enforce_trust(op, {"name": "web-pod-1"})
        confirm_text = result["confirm_text"]
        # Confirm text should reference the operation
        assert "Delete Pod" in confirm_text or "delete-pod" in confirm_text.lower()
