# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the ArgoCD connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from collections.abc import Mapping
from typing import Any

from meho_backplane.op_classes import (
    declare_credential_capable,
    declare_credential_check,
    declare_op_classes,
)

declare_op_classes(
    # The four ArgoCD write verbs (#2681) are exact pins, not new write
    # suffixes: a ``.set`` / ``.sync`` / ``.rollback`` suffix would also
    # reach the flat ``meho_*`` MCP tool names and relabel for example
    # ``meho_broadcast_overrides_set``, and ``.refresh`` is shared with the
    # read-class ``topology.refresh``.
    # ``argocd.app.refresh`` is an HTTP GET that forces ArgoCD to reconcile
    # the live and desired state: an approval-gated action with a
    # cluster-side side effect, so on the approval and audit surface it is
    # a write even though the HTTP verb is a read. The ``.delete`` and
    # ``appproject.create`` / ``.update`` siblings classify write by suffix.
    write=(
        "argocd.app.set",
        "argocd.app.sync",
        "argocd.app.rollback",
        "argocd.app.refresh",
    ),
)

# ``argocd.app.set`` writes the whole Application spec: the repo, the path,
# the Helm values and the cluster it deploys to. ArgoCD then creates what
# that source holds, also pods, roles and Secrets. An AppProject write sets
# which repos, clusters, namespaces and kinds the apps of the project may
# deploy: it gives rights. So every call of these waits for a person, and no
# standing grant, agent grant or run approval covers it.
declare_credential_capable(
    (
        "argocd.app.set",
        "argocd.appproject.create",
        "argocd.appproject.update",
    )
)


def sync_sets_a_revision(params: Mapping[str, Any]) -> bool:
    """True when the ``argocd.app.sync`` call in *params* names its own revision.

    A sync with a revision deploys the branch or commit the caller picks,
    not the target revision the Application already defines. The handler
    sends ``revision`` when the value is truthy, so the same test is used
    here.
    """
    return bool(params.get("revision"))


# A sync to a revision the caller picks waits for a person on every call. A
# sync without one starts what the Application already defines and keeps
# the rules of argocd.app.sync.
declare_credential_check("argocd.app.sync", sync_sets_a_revision)
