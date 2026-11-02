# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the VCF Fleet connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The fleet locker writes. The password and the private key come from
    # Vault at run time and go in the request body; params carry only Vault
    # references. The class keeps the body out of the flight recorder (the
    # generate answer holds the new private key) and the params off the feed.
    credential_write=(
        "fleet.locker.password.add",
        "fleet.locker.certificate.import",
    ),
)
