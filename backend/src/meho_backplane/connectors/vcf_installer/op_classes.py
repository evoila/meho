# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the VCF Installer connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The proxy change and the resource calculation. Their params carry only
    # Vault paths (``password_secret_ref`` / ``password_refs``); the
    # passwords are read in-process and sent only in the request BODY. The
    # class keeps those bodies out of traces and collapses the broadcast.
    credential_write=(
        "installer.system.proxy.set",
        "installer.sddc.resources.calculate",
    ),
)
