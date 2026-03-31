"""Connector type registration and lookup.

Concrete connector modules use @register_connector("type") to register
themselves. The loader uses get_connector_class() to instantiate the right class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meho_claude.core.connectors.base import BaseConnector

# Module-level registry: connector_type -> connector class
_REGISTRY: dict[str, type[BaseConnector]] = {}


def register_connector(connector_type: str):
    """Decorator to register a connector class for a given type.

    Usage::

        @register_connector("rest")
        class RESTConnector(BaseConnector):
            ...
    """

    def decorator(cls: type[BaseConnector]) -> type[BaseConnector]:
        _REGISTRY[connector_type] = cls
        return cls

    return decorator


def get_connector_class(connector_type: str) -> type[BaseConnector]:
    """Look up a registered connector class by type.

    Raises ValueError if the type is not registered.
    """
    if connector_type not in _REGISTRY:
        raise ValueError(
            f"Unknown connector type: {connector_type!r}. "
            f"Available: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[connector_type]


def list_connector_types() -> list[str]:
    """Return sorted list of registered connector type names."""
    return sorted(_REGISTRY.keys())
