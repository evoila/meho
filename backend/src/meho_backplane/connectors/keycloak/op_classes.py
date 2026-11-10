# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Keycloak connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes

declare_op_classes(
    # Takes the realm's SMTP password as a reference
    # (``smtp_password_secret_ref``) and sends the read value to Keycloak.
    # The pin keeps a waiting request's params in the encrypted handoff
    # store, keeps the request bodies out of flight-recorder traces and
    # sends only an aggregate to the feed.
    credential_write=("keycloak.realm.update",),
    # Makes a login flow and binds it to a client. Its params hold only a
    # client id, so it is a plain write. ``.ensure_approval_flow`` is not a
    # write suffix, so it needs this pin.
    # ``.ensure_passkey_code_skip`` copies and binds the realm's browser flow;
    # its params are empty, so it is a plain write too.
    write=(
        "keycloak.authflow.ensure_approval_flow",
        "keycloak.authflow.ensure_passkey_code_skip",
    ),
)
