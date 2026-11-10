# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the BIND 9 connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes, declare_root_shell

declare_op_classes(
    # #4617 -- a BIND config file can hold TSIG and rndc key secrets. The
    # answer is one text field, so the Tier-1 ``bind_key_secret`` rule does
    # the redaction; this class collapses the broadcast and keeps the flight
    # recorder from storing the body.
    credential_read=("bind9.config.show",),
)

# The record and config writes run their script as root through the sudo
# helper of the connector. A run approval never covers these: a person
# approves each call.
declare_root_shell(
    (
        "bind9.record.add",
        "bind9.record.remove",
        "bind9.record.delete",
        "bind9.config.apply_file",
        "bind9.config.apply_views",
        "bind9.config.backup",
        "bind9.config.reload",
    )
)
