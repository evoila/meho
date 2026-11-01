# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the VCF Operations connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The fleet password change. Both passwords come from the secret store
    # at run time, params carry only the two references, and the vendor
    # request body holds the values. The class keeps that body out of the
    # flight recorder and the params off the feed.
    credential_write=("vrops.fleet.password.rotate",),
)
