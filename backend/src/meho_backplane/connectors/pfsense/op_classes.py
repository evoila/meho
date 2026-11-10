# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the pfSense connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes, declare_root_shell

declare_op_classes(
    # VPN onboarding (#3993). ``.sync`` is not a write suffix. ``user.create``
    # makes a password on the firewall and sends it to Vault: the answer holds
    # none, but the op mints a secret.
    credential_mint=("pfsense.openvpn.user.create",),
    # User flag writes (#3937): ``.disable`` / ``.enable`` are not write
    # suffixes, and a suffix would also relabel the flat ``meho_`` mirror.
    # The params carry only a user name, so plain ``write``.
    write=("pfsense.openvpn.csc.sync", "pfsense.user.disable", "pfsense.user.enable"),
)

# The SSH login on pfSense is ``admin``, which is root (uid 0). Every config
# write runs a ``pfSsh.php playback`` script as that user, so it runs a
# command as root on the host. A run approval never covers these: a person
# approves each call. ``pfsense.diag.ping`` also runs over SSH, but it is a
# read with a fixed command and is not marked.
declare_root_shell(
    (
        "pfsense.gateway.add",
        "pfsense.route.static.add",
        "pfsense.filter.rule.add",
        "pfsense.filter.rule.delete",
        "pfsense.user.enable",
        "pfsense.user.disable",
        "pfsense.gateway.delete",
        "pfsense.route.static.delete",
        "pfsense.alias.delete",
        "pfsense.alias.member.remove",
        "pfsense.nat.delete",
        "pfsense.openvpn.csc.sync",
        "pfsense.openvpn.user.create",
        "pfsense.openvpn.user.delete",
    )
)
