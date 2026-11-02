# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Kubernetes connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    credential_read=(
        # The governed guest-cluster kubeconfig read (#3496). Reads a Secret's
        # data value and stages it to a tenant-scoped Vault secret_ref,
        # returning only the ref, never the value. The class makes audit and
        # broadcast collapse to aggregate-only on top of its approval gate.
        "k8s.secret.read_to_ref",
        # #5230 -- the compare read. The backplane reads the secret values
        # itself to compare them and returns only same/different. The class
        # makes the flight recorder drop the vendor bodies (they carry the
        # values) and collapses the broadcast to aggregate-only.
        "k8s.secret.compare_to_ref",
    ),
    credential_write=(
        # The Secret ``data`` / ``stringData`` is in ``params``.
        "k8s.secret.create",
        # The values of an existing Secret ride in the same params shape as
        # k8s.secret.create (inline maps or store refs), so the same class
        # keeps them off the approval row and the broadcast.
        "k8s.secret.update",
        # The Job ``spec`` carries a pod template whose inline ``env`` entries
        # can hold credential material in ``params`` (G3.14-T1 #1403).
        "k8s.job.create",
    ),
)
