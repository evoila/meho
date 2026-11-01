# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Cloud Builder connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The three changes that send a bring-up spec. Their params carry only
    # Vault paths (``secrets``); the passwords are read in-process and go
    # only into the Cloud Builder request BODY. The class keeps those bodies
    # out of the flight recorder and the params out of the broadcast.
    credential_write=(
        "installer.cb.validation.start",
        "installer.cb.sddc.start",
        "installer.cb.sddc.retry",
    ),
)
