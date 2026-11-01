# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The connector kit: the MEHO names a connector package may import.

A connector package imports MEHO only through this module. The names in
``__all__`` are the promise. Every other MEHO module is private to the core
and can change in any release.

The kit has its own version (the version of the ``meho-connector-kit``
package). A package states the kit versions it fits in its
``pyproject.toml``, for example ``dependencies = ["meho-connector-kit>=1.0,<2"]``.

``backend/connector-kit/kit-names.json`` lists every name with its inputs.
``backend/tests/test_connector_kit.py`` fails when the code and that file
differ. To change the kit, run ``uv run python -m
scripts.write_connector_kit_names`` in ``backend/``: it writes the file again
and raises the kit version. See ``docs/codebase/connector-packages.md``,
"The connector kit", and the decision record
``docs/decisions/2026-11-01-connector-kit.md``.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.credential_backend import CredentialsReadError
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vault_creds import (
    VaultCredentialsReadError,
    load_param_secret_data,
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors._shared.vcf_auth import (
    ConnectorAuthError,
    is_acceptable_auth_model,
)
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.base import Connector, ConnectorUpstreamError
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.operations._preview import (
    PermissionPreflight,
    PreviewBuilder,
    PreviewContext,
    register_permission_preflight,
    register_preview_builder,
)
from meho_backplane.operations._validate import compute_audit_digest_hash
from meho_backplane.operations.typed_register import (
    register_typed_op_registrar,
    register_typed_operation,
)
from meho_backplane.retrieval.embedding import EmbeddingService


class Target(Protocol):
    """The target fields a connector may read.

    MEHO hands a connector one of two kinds of target: the database row
    (``meho_backplane.db.models.Target``) or the read model
    (``meho_backplane.targets.schemas.Target``). Both have these six fields.
    Any other field of a target is private to the core.
    """

    @property
    def name(self) -> str:
        """The target's name, unique in its tenant."""
        ...

    @property
    def host(self) -> str:
        """The host name or address MEHO connects to."""
        ...

    @property
    def extras(self) -> Mapping[str, Any]:
        """Settings of the target that only its connector reads."""
        ...

    @property
    def auth_model(self) -> str:
        """How the target logs in: a value of :class:`AuthModel`."""
        ...

    @property
    def verify_tls(self) -> bool:
        """Whether MEHO checks the target's TLS certificate."""
        ...

    @property
    def secret_ref(self) -> str | None:
        """Where the target's secret is in Vault, or ``None``."""
        ...


__all__ = [
    "AuthModel",
    "Connector",
    "ConnectorAuthError",
    "ConnectorUpstreamError",
    "CredentialsReadError",
    "EmbeddingService",
    "FingerprintResult",
    "HttpConnector",
    "OperationResult",
    "Operator",
    "PermissionPreflight",
    "PreviewBuilder",
    "PreviewContext",
    "ProbeResult",
    "Target",
    "VaultClientError",
    "VaultCredentialsReadError",
    "compute_audit_digest_hash",
    "is_acceptable_auth_model",
    "is_system_operator",
    "load_param_secret_data",
    "load_vault_secret_data",
    "register_connector_v2",
    "register_permission_preflight",
    "register_preview_builder",
    "register_typed_op_registrar",
    "register_typed_operation",
    "strip_credential_value",
    "synthesise_system_operator",
    "target_cache_key",
]

#: The parts of a class that the kit promises. For any other class in
#: ``__all__`` the kit promises only the name; its insides may change.
_PROMISED_PARTS: dict[str, tuple[str, ...]] = {
    "HttpConnector": (
        "__init__",
        "auth_headers",
        "aclose",
        "trace_response_body",
        "_get_json",
        "_post_json",
    ),
    "PreviewContext": ("target", "operator", "params", "connector_instance", "connector_id"),
    "Target": ("name", "host", "extras", "auth_model", "verify_tls", "secret_ref"),
}

#: Old kit names: name -> what to use instead. An old name stays for one kit
#: version, then it may go. Only a name listed here in the kit version before
#: may be removed.
_OLD_NAMES: dict[str, str] = {}
