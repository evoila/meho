# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Active Directory connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The AD password set. The password comes from Vault at run time, and
    # params carry only the ``password_ref`` reference. The class keeps even
    # that off the feed, stores the approval input encrypted, and the flight
    # recorder keeps no body for it.
    credential_write=("msad.user.set-password",),
)
