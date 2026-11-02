# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the vRA 8 connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The cloud account add, shared with VCF Automation 9
    # (``vcfa.tenant.cloud_account.create``). Its params carry only a Vault
    # path (``password_secret_ref``); the password is read in-process and
    # sent only in the ``POST /iaas/api/cloud-accounts-vsphere|-nsx-t``
    # request BODY. The class sets body_recorded=false for its
    # flight-recorder spans and collapses the broadcast to aggregate-only.
    credential_write=("vcfa.vra8.cloud_account.create",),
)
