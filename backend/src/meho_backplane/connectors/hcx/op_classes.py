# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the HCX connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # The site pairing. Its params carry only a Vault *path*
    # (``remote_secret_ref``); the remote site's user and password are read
    # in-process and sent only in the ``POST /hybridity/api/cloudConfigs``
    # request BODY. The class sets body_recorded=false for its flight-recorder
    # spans, collapses the broadcast to aggregate-only and keeps a waiting
    # approval's params in the encrypted handoff store.
    credential_write=("hcx.site_pairing.set",),
    # A hand-written ``safe`` operation that is not a plain read: it starts
    # an HCX diagnostics run on the target. Without this class it would get
    # ``read`` from ``resolve_operation_op_class``, like every other
    # ``safe`` hand-written operation.
    write=("hcx.service_mesh.diagnose",),
)
