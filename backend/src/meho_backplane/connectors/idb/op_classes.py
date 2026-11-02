# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Identity Broker connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # ``.sync`` is not a write suffix (see the ArgoCD declaration). The params
    # are a directory id only, so plain ``write``.
    write=("idb.directory.sync",),
)
