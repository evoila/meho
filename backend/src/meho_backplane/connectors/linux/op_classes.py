# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Linux SSH connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

from meho_backplane.op_classes import declare_op_classes, declare_root_shell

declare_op_classes(
    credential_write=(
        # #3361 -- the governed config-file write. Its ``content`` param is
        # the file body to write; ``content`` is neither a secret-named key
        # nor a recognisable secret shape, so the key-name / Tier-1 scrub in
        # ``scrub_broadcast_params`` lets it through. A plain ``write`` class
        # (the ``.write`` suffix) would ship the whole file body on the feed.
        # This class collapses the params dict to aggregate-only; the
        # park-time preview echoes only path, byte size, backup path and
        # validate command, never the content.
        "linux.file.write",
        # #3361 -- the governed script-execution write. Its ``arguments``
        # string (a command line) and ``env`` values can hold credential
        # material (``--token=...``, ``API_KEY=...``) that the scrub misses,
        # and ``.run`` is not a write suffix, so an undeclared class would
        # fall through to ``other`` and ship the command line in full. The
        # park-time preview echoes only program identity, argument byte size
        # and env-var NAMES, never any value.
        "linux.script.run",
    ),
)

# Every write that runs through the sudo helper (``_sudo.py``) runs a command
# as root on the host. A run approval never covers these: a person approves
# each call. ``linux.firewall.show`` also uses the helper, but it is a read
# with a fixed command and never waits for approval.
declare_root_shell(
    (
        "linux.file.write",
        "linux.service.control",
        "linux.script.run",
        "linux.sysctl.write",
        "linux.firewall.load",
        "linux.firewall.confirm",
    )
)
