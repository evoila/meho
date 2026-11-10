# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check that the running BIND server serves the serial of each staged zone file.

``rndc reload`` answers "ok" even when it skips a zone. After
``bind9.config.apply_views`` the verify step therefore asks the server
itself. For every staged file, it finds each zone and view that use the
file. It reads the serial of the file with ``named-checkzone`` (BIND's
own zone file parser), runs ``rndc zonestatus`` and compares the two
serials as numbers. A staged zone file that no configured zone uses
fails the check, because the server does not serve it.

The check runs on the target inside the verify step of the atomic
apply, so a failure rolls the whole change back.
"""

from __future__ import annotations

import base64
import json
import posixpath
import shlex

import dns.exception
import dns.tokenizer

__all__ = ["build_served_serial_check", "is_zone_file"]

# Runs on the target with python3. Arguments: bind root, JSON object
# that maps the absolute path of each staged file to true when it looks
# like a zone file. Every staged file that a configured zone uses gets
# the serial check. A file marked true must be used by a configured zone.
_REMOTE_SCRIPT = r"""
import json
import posixpath
import re
import subprocess
import sys
import time

bind_root = sys.argv[1]
staged = json.loads(sys.argv[2])

conf = subprocess.run(
    ["named-checkconf", "-p"], capture_output=True, text=True, check=True
).stdout

directory = bind_root
view = None
zone = None
depth = 0
view_depth = None
zone_depth = None
found = []  # (view, zone, file)
for line in conf.splitlines():
    text = line.strip()
    m = re.match(r'view\s+"([^"]+)"', text)
    if m and zone is None:
        view, view_depth = m.group(1), depth
    m = re.match(r'zone\s+"([^"]+)"', text)
    if m:
        zone, zone_depth = m.group(1), depth
    m = re.match(r'directory\s+"([^"]+)"\s*;', text)
    if m and view is None and zone is None:
        directory = m.group(1)
    m = re.match(r'file\s+"([^"]+)"\s*;', text)
    if m and zone is not None:
        path = m.group(1)
        if not path.startswith("/"):
            path = posixpath.join(directory, path)
        found.append((view, zone, posixpath.normpath(path)))
    depth += text.count("{") - text.count("}")
    if zone is not None and depth <= zone_depth:
        zone = None
    if view is not None and zone is None and depth <= view_depth:
        view = None


def file_serial(zone_name, path):
    run = subprocess.run(
        ["named-checkzone", "-w", directory, zone_name, path],
        capture_output=True,
        text=True,
    )
    m = re.search(r"loaded serial (\d+)", run.stdout)
    if run.returncode != 0 or m is None:
        return None, (run.stdout + run.stderr).strip()
    return int(m.group(1)), ""


def served_serial(zone_name, zone_view):
    cmd = ["rndc", "zonestatus", zone_name, "IN"]
    if zone_view:
        cmd.append(zone_view)
    run = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"^serial:\s*(\d+)", run.stdout, re.MULTILINE)
    return int(m.group(1)) if m else None


problems = []
for path, is_zone in sorted(staged.items()):
    users = [(v, z) for v, z, f in found if f == path]
    if not users:
        if is_zone:
            problems.append("file " + path + ": no configured zone uses this file")
        continue
    for zone_view, zone_name in users:
        label = zone_name + (" (view " + zone_view + ")" if zone_view else "")
        want, error = file_serial(zone_name, path)
        if want is None:
            problems.append("zone " + label + ": named-checkzone cannot read " + path
                            + ": " + error)
            continue
        got = None
        for _ in range(10):
            got = served_serial(zone_name, zone_view)
            if got == want:
                break
            time.sleep(1)
        if got != want:
            problems.append(
                "zone " + label + ": file has serial " + str(want)
                + ", server serves " + str(got)
            )
if problems:
    print("the server does not serve the new zone files:")
    print("\n".join(problems))
    sys.exit(1)
"""


def _is_config_file_name(path: str) -> bool:
    name = posixpath.basename(path)
    return name == "named.conf" or name.startswith("named.conf.") or name.endswith(".conf")


def is_zone_file(path: str, content: str) -> bool:
    """Return ``True`` when *content* is a zone file with a SOA record.

    The DNS tokenizer skips comments and handles ``(`` and quoted text,
    so a SOA in a comment does not count. Config files (``named.conf``,
    ``named.conf.*``, ``*.conf``) are never zone files. Content that the
    tokenizer cannot read counts as a zone file, so the server-side check
    still runs for it.
    """
    if _is_config_file_name(path):
        return False
    tokenizer = dns.tokenizer.Tokenizer(content)
    try:
        while not (token := tokenizer.get()).is_eof():
            if token.is_identifier() and token.value.upper() == "SOA":
                return True
    except dns.exception.DNSException:
        return True
    return False


def build_served_serial_check(
    staged: dict[str, bool],
    *,
    bind_root: str,
) -> str | None:
    """Build the shell command for the served-serial check.

    *staged* maps the absolute path of each staged file to ``True`` when
    it looks like a zone file (see :func:`is_zone_file`). The server
    checks the serial of every staged file that a configured zone uses,
    whatever its name or content, so an empty or misnamed zone file still
    fails. The flag only decides whether a file that no zone uses fails.
    Returns ``None`` when no file is staged.
    """
    if not staged:
        return None
    script_b64 = base64.b64encode(_REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
    return (
        f"printf %s {script_b64} | base64 -d "
        f"| python3 - {shlex.quote(bind_root)} {shlex.quote(json.dumps(staged))}"
    )
