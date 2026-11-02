# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the VCF Automation connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # #3890 -- the API-token mint. It logs in as the org user and mints a
    # long-lived refresh token; the handler writes the token straight to
    # Vault and returns only a Vault pointer and a SHA-256. The class
    # collapses the broadcast to aggregate-only and makes the flight
    # recorder drop its bodies (the OAuth form carries the session JWT; the
    # token response carries the token).
    credential_mint=("vcfa.provider.api_token.create",),
    credential_write=(
        # #3890 -- the local org-user create. Its params carry only a Vault
        # path (``password_secret_ref``); the password is read in-process and
        # sent in the request BODY. The class sets body_recorded=false for
        # its flight-recorder spans and collapses the broadcast.
        "vcfa.provider.user.create",
        # #3890 -- the API-token revoke logs in as the token's owner with the
        # password read from ``password_secret_ref``. ``.revoke`` would
        # otherwise fall through to ``other`` and broadcast in full.
        "vcfa.provider.api_token.revoke",
    ),
    # A hand-written ``safe`` operation that is not a plain read: it makes
    # the target reload its image list. Without this class it would get
    # ``read`` from ``resolve_operation_op_class``.
    write=("vcfa.tenant.cloud_account.enumerate_images",),
)
