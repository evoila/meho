# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.github — GitHubRestConnector package (G3.11-T1).

Importing the package registers :class:`GitHubRestConnector` against
**both** the v1 single-product registry and the v2 three-tuple registry:

* **v1 entry** — ``register_connector("gh", GitHubRestConnector)``.
  Writes the wildcard ``("gh", "", "")`` triple into the v2 table as a
  side effect so unfingerprinted ``(product="gh", version=None)``
  targets resolve to this connector via the G0.15-T6 wildcard tie-break
  ladder. The v1 lookup itself is what
  :func:`~meho_backplane.api.v1.health._probe_*` paths key on.
* **v2 entry** — ``register_connector_v2(product="gh", version="3",
  impl_id="gh-rest", cls=GitHubRestConnector)``. The canonical
  dispatcher key. The resolver's tie-break ladder
  (G0.14-T2 #1143 step 1, ``versioned_over_wildcard``) demotes the v1
  wildcard whenever this versioned entry is also a candidate, so a
  fingerprinted ``(product="gh", version="3")`` target resolves cleanly
  to the versioned class.

Per G0.15-T6 (mandatory pattern), every new connector ships dual
registration from day one. This is the same shape
:mod:`meho_backplane.connectors.kubernetes` uses.

Typed-op registration is intentionally a no-op at this Task — T1 ships
the substrate; T3 (#1223) ships the catalog entry + Layer-2 ingest
acceptance which populates the ``endpoint_descriptor`` rows. The
:meth:`GitHubRestConnector.register_operations` classmethod stays a stub
the lifespan can call cheaply; T3 fills in the body.
"""

from meho_backplane.connectors.github.connector import GitHubRestConnector
from meho_backplane.connectors.github.session import (
    DEFAULT_GITHUB_API_URL,
    GITHUB_APP_AUTH_MODEL,
    GITHUB_PAT_AUTH_MODEL,
    GitHubAppCredentials,
    GitHubAppNotInstalledError,
    GitHubCredentialError,
    GitHubCredentialsLoader,
    GitHubInstallationTokenMintError,
    GitHubJWTMintError,
    GitHubPATCredentials,
    GitHubRateLimitedError,
    GitHubTargetLike,
    InstallationToken,
    load_github_app_credentials_from_vault,
    load_github_pat_credentials_from_vault,
    mint_github_app_jwt,
)
from meho_backplane.connectors.registry import (
    register_connector,
    register_connector_v2,
)
from meho_backplane.operations.typed_register import register_typed_op_registrar


async def register_github_typed_operations(
    *,
    embedding_service: object | None = None,
) -> None:
    """No-op registrar — T1 ships zero typed ops.

    Queued onto :func:`run_typed_op_registrars` for symmetry with vault /
    kubernetes / vmware-rest so a future enhancement (T3's catalog
    entry) can fill in the body without touching the lifespan wiring.
    The pattern matches :func:`register_kubernetes_typed_operations` —
    a module-level wrapper around the connector's classmethod.

    The ``embedding_service`` keyword-only parameter mirrors every other
    registrar's contract: :func:`run_typed_op_registrars` passes the
    process-wide :class:`EmbeddingService` (or a chassis-test stub) to
    every registrar via ``registrar(embedding_service=...)``, so each
    registrar **must** accept the kwarg or the lifespan crashes with
    ``TypeError`` (see the K8s precedent for the post-#461/#463
    ordering incident). T1 doesn't register anything yet so the value
    is unused; T3 will forward it to its embedding-text encode path.
    """
    del embedding_service  # unused until T3 fills in the body
    await GitHubRestConnector.register_operations()


__all__ = [
    "DEFAULT_GITHUB_API_URL",
    "GITHUB_APP_AUTH_MODEL",
    "GITHUB_PAT_AUTH_MODEL",
    "GitHubAppCredentials",
    "GitHubAppNotInstalledError",
    "GitHubCredentialError",
    "GitHubCredentialsLoader",
    "GitHubInstallationTokenMintError",
    "GitHubJWTMintError",
    "GitHubPATCredentials",
    "GitHubRateLimitedError",
    "GitHubRestConnector",
    "GitHubTargetLike",
    "InstallationToken",
    "load_github_app_credentials_from_vault",
    "load_github_pat_credentials_from_vault",
    "mint_github_app_jwt",
    "register_github_typed_operations",
]

# v1 entry — wildcard ("gh", "", "") via :func:`register_connector` per
# G0.15-T6. Unfingerprinted targets carrying ``(product="gh",
# version=None)`` resolve here via the resolver's wildcard tie-break.
register_connector("gh", GitHubRestConnector)

# v2 entry — versioned ("gh", "3", "gh-rest"). The canonical resolver
# key for dispatch; the tie-break ladder demotes the v1 wildcard when
# both candidates match the same target.
register_connector_v2(
    product="gh",
    version="3",
    impl_id="gh-rest",
    cls=GitHubRestConnector,
)

# Queue the typed-op registrar onto the lifespan-driven list. T1 ships
# a no-op; T3 will fill in the body once the catalog entry lands.
register_typed_op_registrar(register_github_typed_operations)
