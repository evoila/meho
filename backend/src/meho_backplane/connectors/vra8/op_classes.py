# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Aria Automation 8 connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The integration add or change. Its params carry only a Vault path
    # (``password_secret_ref``); the password or token is read in-process and
    # sent only in the ``POST``/``PATCH /iaas/api/integrations`` request BODY.
    credential_write=("vcfa.vra8.integration.set",),
)
