# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The op classes the Linux SSH connector declares for its own operations.

See :mod:`meho_backplane.op_classes`. The broadcast classifier reads these
before its pattern rules.
"""

import re
from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.op_classes import (
    declare_credential_check,
    declare_op_classes,
    declare_root_shell,
)

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
        # park-time preview echoes the script, the argument byte size and the
        # ``env`` values the approver must see; never the argument values.
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

#: The names of the ``kernel`` parameters whose value names a program the
#: kernel runs as root (``modprobe``, ``hotplug``, ``poweroff_cmd``) or a file
#: it writes (``core_pattern``). A key whose last part is one of them waits,
#: whatever its other parts are.
RUN_OR_WRITE_NAMES: Final[frozenset[str]] = frozenset(
    {"core_pattern", "hotplug", "modprobe", "poweroff_cmd"}
)

#: A directory of kernel parameters that registers a program to run for a
#: kind of file.
_BINFMT_MISC: Final[str] = "binfmt_misc"

_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[./]")


def sysctl_write_can_run_a_program(params: Mapping[str, Any]) -> bool:
    """True when the ``linux.sysctl.write`` call in *params* can make the kernel run or write.

    ``sysctl`` reads ``.`` and ``/`` both as separators, so the key is split
    on both. The call is flagged when:

    * the key starts with ``-``: ``sysctl`` reads it as an option (for
      example ``--load``), not as a parameter name,
    * the value has a ``/`` (a path the kernel can run or write to),
    * a part of the key is ``binfmt_misc``,
    * the last part of the key is in :data:`RUN_OR_WRITE_NAMES`,
    * the key has an empty part (a repeated separator, a ``.`` or ``..``
      step, a separator at the start or end): MEHO does not read such a key
      as a plain parameter name.

    A key or a value that is not text counts too. The key is compared in
    lower case.
    """
    key = params["key"]
    value = params["value"]
    if not isinstance(key, str) or not isinstance(value, str):
        return True
    if key.startswith("-"):
        return True
    if "/" in value:
        return True
    parts = _SEPARATORS.split(key.lower())
    if any(part == "" for part in parts):
        return True
    if _BINFMT_MISC in parts:
        return True
    return parts[-1] in RUN_OR_WRITE_NAMES


# A kernel parameter that names a program the kernel runs, or a file it
# writes, waits for a person on every call: no standing grant, agent grant
# or run approval covers it. A plain tuning key with a number keeps the
# rules of linux.sysctl.write.
declare_credential_check("linux.sysctl.write", sysctl_write_can_run_a_program)
