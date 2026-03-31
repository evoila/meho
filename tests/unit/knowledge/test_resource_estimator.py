# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for ResourceEstimator page-to-resource mapping."""

import pytest

from meho_app.worker.backends.protocol import ResourceProfile
from meho_app.worker.resource_estimator import estimate_resources


class TestEstimateResources:
    """Page count to ResourceProfile mapping tests."""

    def test_tiny_5_pages(self) -> None:
        """estimate_resources(5) returns size_category='tiny', memory_gb=8 (6GB min for 1 page)."""
        profile = estimate_resources(5)
        assert profile.size_category == "tiny"
        assert profile.memory_gb == 8

    def test_tiny_boundary_10_pages(self) -> None:
        """estimate_resources(10) returns size_category='tiny' (boundary)."""
        profile = estimate_resources(10)
        assert profile.size_category == "tiny"

    def test_small_11_pages(self) -> None:
        """estimate_resources(11) returns size_category='small', memory_gb=8."""
        profile = estimate_resources(11)
        assert profile.size_category == "small"
        assert profile.memory_gb == 8

    def test_small_boundary_50_pages(self) -> None:
        """estimate_resources(50) returns size_category='small' (boundary)."""
        profile = estimate_resources(50)
        assert profile.size_category == "small"

    def test_medium_boundary_500_pages(self) -> None:
        """estimate_resources(500) returns size_category='medium', memory_gb=16."""
        profile = estimate_resources(500)
        assert profile.size_category == "medium"
        assert profile.memory_gb == 16

    def test_large_boundary_2000_pages(self) -> None:
        """estimate_resources(2000) returns size_category='large', memory_gb=16."""
        profile = estimate_resources(2000)
        assert profile.size_category == "large"
        assert profile.memory_gb == 16

    def test_huge_8000_pages(self) -> None:
        """estimate_resources(8000) returns size_category='huge', memory_gb=32."""
        profile = estimate_resources(8000)
        assert profile.size_category == "huge"
        assert profile.memory_gb == 32

    def test_huge_extreme(self) -> None:
        """estimate_resources(999999) returns size_category='huge'."""
        profile = estimate_resources(999999)
        assert profile.size_category == "huge"

    def test_all_profiles_have_positive_timeout(self) -> None:
        """All profiles have timeout_seconds > 0."""
        for pages in [1, 10, 11, 50, 51, 500, 501, 2000, 2001, 999999]:
            profile = estimate_resources(pages)
            assert profile.timeout_seconds > 0, f"pages={pages} has timeout={profile.timeout_seconds}"

    def test_resource_profile_is_frozen(self) -> None:
        """ResourceProfile is frozen (immutable)."""
        profile = estimate_resources(5)
        assert isinstance(profile, ResourceProfile)
        with pytest.raises(AttributeError):
            profile.memory_gb = 99  # type: ignore[misc]
