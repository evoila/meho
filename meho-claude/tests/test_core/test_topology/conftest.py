"""Shared fixtures for topology test modules.

The autouse fixture ensures all real extractor modules are imported (triggering
@register_extractor decorators) before every test, and restores the registry
to that known-good state after every test. This prevents cross-test pollution
from tests that register mock extractors.
"""

import pytest

from meho_claude.core.topology.extractor import _EXTRACTOR_REGISTRY


def _ensure_extractors_imported():
    """Import all extractor modules to trigger @register_extractor decorators."""
    try:
        import meho_claude.core.topology.extractors  # noqa: F401
    except ImportError:
        pass


# Trigger real extractor registration at module load time
_ensure_extractors_imported()


@pytest.fixture(autouse=True)
def _ensure_extractors_registered():
    """Save and restore extractor registry around every topology test.

    Before each test: ensure real extractors are registered.
    After each test: restore registry to the known-good state (with real extractors).
    This prevents mock extractor registrations from leaking between tests.
    """
    _ensure_extractors_imported()
    saved = dict(_EXTRACTOR_REGISTRY)
    yield
    _EXTRACTOR_REGISTRY.clear()
    _EXTRACTOR_REGISTRY.update(saved)
