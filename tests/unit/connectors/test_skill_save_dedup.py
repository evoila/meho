# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for save-time skill dedup logic.

D-02: When custom_skill content matches generated_skill, the save endpoint
should clear custom_skill to NULL to prevent silent duplication.

These tests verify the dedup comparison logic extracted into a helper function
(`_dedup_custom_skill`) rather than testing through the full endpoint, to avoid
needing real database sessions and auth dependencies.
"""

import pytest

from meho_app.api.connectors.operations.skills import _dedup_custom_skill


class TestSaveTimeSkillDedup:
    """Tests for _dedup_custom_skill() helper.

    The helper returns the value that should be stored as custom_skill:
    - None when custom matches generated (dedup)
    - The original custom_skill value otherwise
    """

    def test_save_clears_duplicate_custom_skill(self):
        """When custom_skill.strip() == generated_skill.strip(), return None."""
        result = _dedup_custom_skill(
            custom_skill="# Kubernetes Skill\n\nManage pods.",
            generated_skill="# Kubernetes Skill\n\nManage pods.",
        )
        assert result is None

    def test_save_preserves_different_custom_skill(self):
        """When content differs, return custom_skill as-is."""
        custom = "# Custom operator notes about this cluster"
        result = _dedup_custom_skill(
            custom_skill=custom,
            generated_skill="# Generated Kubernetes Skill",
        )
        assert result == custom

    def test_save_preserves_when_generated_skill_is_none(self):
        """When generated_skill is NULL, custom_skill is always saved."""
        custom = "# Custom skill for REST connector"
        result = _dedup_custom_skill(
            custom_skill=custom,
            generated_skill=None,
        )
        assert result == custom

    def test_save_handles_empty_custom_skill(self):
        """When custom_skill is empty string, it is saved as-is."""
        result = _dedup_custom_skill(
            custom_skill="",
            generated_skill="# Generated skill",
        )
        assert result == ""

    def test_save_dedup_strips_whitespace(self):
        """Trailing/leading whitespace differences do not prevent dedup."""
        result = _dedup_custom_skill(
            custom_skill="  # Skill Content  \n\n",
            generated_skill="# Skill Content",
        )
        assert result is None

    def test_save_preserves_none_custom_skill(self):
        """When custom_skill is None, return None (no-op)."""
        result = _dedup_custom_skill(
            custom_skill=None,
            generated_skill="# Generated skill",
        )
        assert result is None

    def test_save_preserves_when_both_none(self):
        """When both are None, return None."""
        result = _dedup_custom_skill(
            custom_skill=None,
            generated_skill=None,
        )
        assert result is None
