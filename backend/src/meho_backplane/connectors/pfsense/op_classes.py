# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the pfSense connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # User flag writes (#3937): ``.disable`` / ``.enable`` are not write
    # suffixes, and a suffix would also relabel the flat ``meho_`` mirror.
    # The params carry only a user name, so plain ``write``.
    write=("pfsense.user.disable", "pfsense.user.enable"),
)
