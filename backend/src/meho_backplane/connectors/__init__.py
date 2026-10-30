# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors — connector contract public API.

Re-exports the ABC, all result models, the registry functions (v1 + v2),
and the resolver so downstream consumers can import from this package
root.
"""

from pathlib import Path

from meho_backplane.connector_packages import (
    check_connector_folders,
    record_installed_connector_packages,
)

# This must stay the first thing this package does. Python runs this
# ``__init__`` before any ``connectors.<folder>`` module, whoever imports
# it: the registry walk, a route module in ``main.py``, or a test. The
# imports below already pull in ``connectors._shared``, so the check has
# to come before them. The check itself lives outside this folder: a
# folder added under ``connectors/`` would shadow a module of the same
# name here, and Python would run it in place of the check. See
# ``docs/codebase/connector-packages.md``.
record_installed_connector_packages(check_connector_folders(Path(next(iter(__path__)))))

from meho_backplane.connectors.base import Connector, ShimKind, shim_kind  # noqa: E402
from meho_backplane.connectors.profile import (  # noqa: E402
    NAMED_AUTH_SCHEMES,
    RESERVED_AUTH_SCHEMES,
    VERSION_SPLITTERS,
    AuthSchemeName,
    AuthSpec,
    CursorTokenPagination,
    ExecutionProfile,
    ExecutionProfileError,
    FingerprintSpec,
    PaginationSpec,
    ProbeSpec,
    ReservedAuthSchemeError,
    StaticHeaderValueKind,
    UnknownAuthSchemeError,
    VersionSplitter,
    split_version,
    validate_execution_profile,
)
from meho_backplane.connectors.profiled import ProfiledRestConnector  # noqa: E402
from meho_backplane.connectors.registry import (  # noqa: E402
    all_connectors,
    all_connectors_v2,
    get_connector,
    get_connector_v2,
    list_connector_impls,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import (  # noqa: E402
    AmbiguousConnectorResolution,
    NoMatchingConnector,
    ResolutionLabel,
    resolve_connector,
    resolve_connector_or_label,
    resolve_target_version,
)
from meho_backplane.connectors.schemas import (  # noqa: E402
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
    "NAMED_AUTH_SCHEMES",
    "RESERVED_AUTH_SCHEMES",
    "VERSION_SPLITTERS",
    "AmbiguousConnectorResolution",
    "AuthModel",
    "AuthSchemeName",
    "AuthSpec",
    "CandidateHint",
    "Connector",
    "CursorTokenPagination",
    "EdgeHint",
    "EdgeKind",
    "ExecutionProfile",
    "ExecutionProfileError",
    "FingerprintResult",
    "FingerprintSpec",
    "NoMatchingConnector",
    "NodeHint",
    "NodeKind",
    "OperationResult",
    "PaginationSpec",
    "ProbeResult",
    "ProbeSpec",
    "ProfiledRestConnector",
    "ReservedAuthSchemeError",
    "ResolutionLabel",
    "ResultHandle",
    "ShimKind",
    "StaticHeaderValueKind",
    "TopologyHints",
    "UnknownAuthSchemeError",
    "VersionSplitter",
    "all_connectors",
    "all_connectors_v2",
    "get_connector",
    "get_connector_v2",
    "list_connector_impls",
    "register_connector",
    "register_connector_v2",
    "resolve_connector",
    "resolve_connector_or_label",
    "resolve_target_version",
    "shim_kind",
    "split_version",
    "validate_execution_profile",
]
