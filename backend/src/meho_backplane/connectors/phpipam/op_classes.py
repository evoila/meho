# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the phpIPAM connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # Address writes: ``.reserve`` / ``.release`` are not write suffixes. The
    # params carry a subnet id, an address and a host name only, so plain
    # ``write``.
    write=("phpipam.address.reserve", "phpipam.address.release"),
)
