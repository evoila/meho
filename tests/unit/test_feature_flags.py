# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for FeatureFlags module."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear lru_cache between tests so env var changes take effect."""
    try:
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
    except ImportError:
        pass
    yield
    try:
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
    except ImportError:
        pass


class TestFeatureFlagsDefaults:
    """All flags default to True when no env vars are set."""

    def test_knowledge_defaults_true(self, monkeypatch):
        # Ensure the env var is NOT set
        monkeypatch.delenv("MEHO_FEATURE_KNOWLEDGE", raising=False)
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.knowledge is True

    def test_topology_defaults_true(self, monkeypatch):
        monkeypatch.delenv("MEHO_FEATURE_TOPOLOGY", raising=False)
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.topology is True

    def test_scheduled_tasks_defaults_true(self, monkeypatch):
        monkeypatch.delenv("MEHO_FEATURE_SCHEDULED_TASKS", raising=False)
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.scheduled_tasks is True

    def test_events_defaults_true(self, monkeypatch):
        monkeypatch.delenv("MEHO_FEATURE_EVENTS", raising=False)
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.events is True

    def test_memory_defaults_true(self, monkeypatch):
        monkeypatch.delenv("MEHO_FEATURE_MEMORY", raising=False)
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.memory is True


class TestFeatureFlagsOverrides:
    """Each flag can be disabled via env var."""

    def test_knowledge_disabled(self, monkeypatch):
        monkeypatch.setenv("MEHO_FEATURE_KNOWLEDGE", "false")
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.knowledge is False

    def test_topology_disabled(self, monkeypatch):
        monkeypatch.setenv("MEHO_FEATURE_TOPOLOGY", "false")
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.topology is False

    def test_scheduled_tasks_disabled(self, monkeypatch):
        monkeypatch.setenv("MEHO_FEATURE_SCHEDULED_TASKS", "false")
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.scheduled_tasks is False

    def test_events_disabled(self, monkeypatch):
        monkeypatch.setenv("MEHO_FEATURE_EVENTS", "false")
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.events is False

    def test_memory_disabled(self, monkeypatch):
        monkeypatch.setenv("MEHO_FEATURE_MEMORY", "false")
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.memory is False


class TestFeatureFlagsCaseInsensitive:
    """Case-insensitive values work (False, FALSE, false all produce False)."""

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "0"])
    def test_case_insensitive_false(self, monkeypatch, value):
        monkeypatch.setenv("MEHO_FEATURE_KNOWLEDGE", value)
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.knowledge is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1"])
    def test_case_insensitive_true(self, monkeypatch, value):
        monkeypatch.setenv("MEHO_FEATURE_KNOWLEDGE", value)
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        assert flags.knowledge is True


class TestFeatureFlagsFrozen:
    """FeatureFlags is frozen (cannot be mutated after creation)."""

    def test_frozen_raises_on_mutation(self):
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags = get_feature_flags()
        with pytest.raises(Exception):
            flags.knowledge = False


class TestFeatureFlagsSingleton:
    """get_feature_flags() returns same instance (cached)."""

    def test_singleton_caching(self):
        from meho_app.core.feature_flags import get_feature_flags

        get_feature_flags.cache_clear()
        flags1 = get_feature_flags()
        flags2 = get_feature_flags()
        assert flags1 is flags2
