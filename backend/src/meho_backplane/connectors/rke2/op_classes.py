# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the RKE2 connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # G-Node/RKE2-T2 #2429 -- the server-token rotate mints a new join token
    # server-side. The handler never returns the token value (only a Vault
    # pointer), but ``.rotate`` would otherwise classify ``other`` and
    # broadcast full params and detail. The class collapses the broadcast
    # to aggregate-only as defence-in-depth.
    credential_mint=("rke2.token.rotate",),
    # G-Node/RKE2-T3 #2430 -- the node-config write. Its ``patch`` param
    # carries key -> value edits, and an RKE2 ``config.yaml`` body holds
    # ``token:`` / ``agent-token:`` join credentials, so a plain ``write``
    # class (the ``.update`` suffix) would broadcast a written token in
    # full. The op result itself returns key NAMES only.
    credential_write=("rke2.node.config.update",),
    # A hand-written ``safe`` operation that is not a plain read: it makes
    # the target save an etcd snapshot. Without this class it would get
    # ``read`` from ``resolve_operation_op_class``.
    write=("rke2.etcd-snapshot.save",),
)
