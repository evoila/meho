# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors — connector contract public API.

Re-exports the ABC, all result models, the registry functions (v1 + v2),
and the resolver so downstream consumers can import from this package
root.
"""

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import (
    all_connectors,
    all_connectors_v2,
    get_connector,
    list_connector_impls,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import (
    AmbiguousConnectorResolution,
    NoMatchingConnector,
    ResolutionLabel,
    resolve_connector,
    resolve_connector_or_label,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    CandidateHint,
    EdgeHint,
    EdgeKind,
    FingerprintResult,
    NodeHint,
    NodeKind,
    OperationResult,
    ProbeResult,
    ResultHandle,
    TopologyHints,
)

__all__ = [
    "AmbiguousConnectorResolution",
    "AuthModel",
    "CandidateHint",
    "Connector",
    "EdgeHint",
    "EdgeKind",
    "FingerprintResult",
    "NoMatchingConnector",
    "NodeHint",
    "NodeKind",
    "OperationResult",
    "ProbeResult",
    "ResolutionLabel",
    "ResultHandle",
    "TopologyHints",
    "all_connectors",
    "all_connectors_v2",
    "get_connector",
    "list_connector_impls",
    "register_connector",
    "register_connector_v2",
    "resolve_connector",
    "resolve_connector_or_label",
]
