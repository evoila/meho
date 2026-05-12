# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.kubernetes — KubernetesConnector package.

Importing the package registers :class:`KubernetesConnector` against
the connector registry when one is available. The registry itself
lands with G0.2-T2 (PR #295, issue #241); until that PR merges, the
registration block degrades to a no-op import-time log line so the
package is still importable. Once #241 lands, registration succeeds
automatically — no code change in this module is required.
"""

from meho_backplane.connectors.kubernetes.connector import (
    KubernetesConnector,
    product_from_git_version,
)
from meho_backplane.connectors.kubernetes.kubeconfig import (
    KubeconfigLoader,
    KubernetesTargetLike,
    load_kubeconfig_from_vault,
    parse_kubeconfig_yaml,
)

__all__ = [
    "KubeconfigLoader",
    "KubernetesConnector",
    "KubernetesTargetLike",
    "load_kubeconfig_from_vault",
    "parse_kubeconfig_yaml",
    "product_from_git_version",
]

try:
    # The registry lands with G0.2-T2 (#241, PR #295). Until that PR
    # merges into main, the module does not exist; the ``type: ignore``
    # is the targeted suppression. Drop it when #241 lands.
    from meho_backplane.connectors.registry import (  # type: ignore[import-untyped, unused-ignore]
        register_connector,
    )
except ImportError:  # pragma: no cover — exercised once G0.2-T2 (#241) lands.
    import structlog

    structlog.get_logger(__name__).info(
        "connector_registry_deferred",
        product="kubernetes",
        reason="meho_backplane.connectors.registry not yet importable (G0.2-T2 / #241 open)",
    )
else:
    register_connector("kubernetes", KubernetesConnector)
