# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the ArgoCD connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

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
