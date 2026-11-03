# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check every connector folder for ``sudo`` command lines.

This check scans the source of every connector, not only bind9. It lives
in its own file so that the one-connector unit run
(``tests/_unit_connector_select.py``) can run it for any connector without
running the whole bind9 suite.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_sudo_is_only_referenced_via_the_safe_primitive() -> None:
    """No other sudo-argv-construction path lives in the connector layer.

    Walks every ``*.py`` under
    ``backend/src/meho_backplane/connectors/`` and asserts no string
    literal opens with ``sudo`` as its first token (the universal shape
    of sudo argv construction: ``"sudo -S ..."`` single-string remote
    commands, ``["sudo", "-S", ...]`` list-form argv,
    ``("sudo",)`` tuple-form). The only files allowed to carry such
    a literal are :mod:`~meho_backplane.connectors.bind9.connector`
    (the ``_remote_bash_with_sudo`` helper),
    :mod:`~meho_backplane.connectors.bind9._atomic` (the atomic-apply
    primitive that funnels its writes through the helper),
    :mod:`~meho_backplane.connectors.rke2._sudo` (the rke2 connector's
    own sanctioned safe-sudo primitive — same wire shape as bind9's
    ``_remote_bash_with_sudo``: it stdin-streams a bash script body to
    ``sudo -S -p "" bash`` so the password never lands in argv/logs), and
    :mod:`~meho_backplane.connectors.linux._sudo` (the linux-ssh
    connector's copy of that same byte-identical primitive).

    A failure means a new sudo argv slipped in somewhere; the
    operator must fold it back through :meth:`_remote_bash_with_sudo`.

    Detection regex — ``(?<=['"])sudo(?=['"\\s\\\\])`` — matches
    ``sudo`` only when immediately preceded by a quote AND followed
    by a quote / whitespace / backslash. Rejects prose mentions
    ("a sudo credential"), docstring narrative, and the identifier
    ``sudo_password`` (no preceding quote). Covers both
    ``"sudo -S ..."`` and the list-form ``["sudo", "-S", ...]``;
    a looser ``\\bsudo\\b`` would false-positive on every docstring
    mention, a stricter ``\\bsudo\\s+`` would miss the list form.
    """
    connectors_root = (
        Path(__file__).resolve().parent.parent / "src" / "meho_backplane" / "connectors"
    )
    # Narrow whitelist: only the files that own a sanctioned safe-sudo
    # primitive. ops_*.py files reference sudo only via docstrings /
    # error messages / the ``"sudo_password"`` dict key — all of which
    # the detection regex below correctly ignores.
    allowed_files = {
        connectors_root / "bind9" / "connector.py",
        connectors_root / "bind9" / "_atomic.py",
        connectors_root / "rke2" / "_sudo.py",
        # The linux-ssh connector's own sanctioned safe-sudo primitive --
        # byte-identical wire shape to rke2/_sudo.py (stdin-streams the bash
        # body to ``sudo -S -p "" bash`` so the password never lands in argv).
        connectors_root / "linux" / "_sudo.py",
    }
    sudo_argv_re = re.compile(r"""(?<=["'])sudo(?=["'\s\\])""")
    offenders: list[str] = []
    for py_file in connectors_root.rglob("*.py"):
        if py_file in allowed_files:
            continue
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            if sudo_argv_re.search(line):
                offenders.append(f"{py_file.relative_to(connectors_root)}:{lineno}: {line!r}")
    assert not offenders, (
        "Found sudo-argv construction outside the bind9 safe-primitive surface "
        "(connector.py + _atomic.py). Every match below re-introduces the "
        "mis-ordered-payload shape that leaked credentials twice in 2026-05:\n"
        + "\n".join(offenders)
    )
