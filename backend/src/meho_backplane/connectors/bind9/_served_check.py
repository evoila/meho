# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check that the running BIND server serves the serial of each staged zone file.

``rndc reload`` answers "ok" even when it skips a zone. After
``bind9.config.apply_views`` the verify step therefore asks the server
itself: for every staged file that holds a SOA record, find the zone
and the view that use the file, run ``rndc zonestatus`` and compare the
serial the server reports with the serial in the file.

The check runs on the target inside the verify step of the atomic
apply, so a mismatch rolls the whole change back.
"""

from __future__ import annotations

import base64
import json
import re
import shlex

__all__ = ["build_served_serial_check", "soa_serial"]

_SOA_SERIAL = re.compile(
    r"\bSOA\b\s+\S+\s+\S+\s*\(?\s*(?:;[^\n]*\n\s*)*(\d+)",
    re.IGNORECASE,
)

# Runs on the target with python3. Arguments: bind root, JSON object
# that maps an absolute file path to its expected serial.
_REMOTE_SCRIPT = r"""
import json
import posixpath
import re
import subprocess
import sys
import time

bind_root = sys.argv[1]
expected = json.loads(sys.argv[2])

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

problems = []
for path, want in sorted(expected.items()):
    for zone_view, zone_name, zone_file in found:
        if zone_file != path:
            continue
        cmd = ["rndc", "zonestatus", zone_name, "IN"]
        if zone_view:
            cmd.append(zone_view)
        label = zone_name + (" (view " + zone_view + ")" if zone_view else "")
        got = None
        for _ in range(10):
            run = subprocess.run(cmd, capture_output=True, text=True)
            m = re.search(r"^serial:\s*(\d+)", run.stdout, re.MULTILINE)
            got = m.group(1) if m else None
            if got == str(want):
                break
            time.sleep(1)
        if got != str(want):
            problems.append(
                "zone " + label + ": file has serial " + str(want)
                + ", server serves " + str(got)
            )
if problems:
    print("the server does not serve the new zone files:")
    print("\n".join(problems))
    sys.exit(1)
"""


def soa_serial(content: str) -> str | None:
    """Return the SOA serial in *content*, or ``None`` when it has no SOA record."""
    match = _SOA_SERIAL.search(content)
    return match.group(1) if match else None


def build_served_serial_check(
    expected: dict[str, str],
    *,
    bind_root: str,
) -> str | None:
    """Build the shell command for the served-serial check.

    *expected* maps the absolute path of a staged zone file to its SOA
    serial. Returns ``None`` when it is empty (no zone file staged).
    """
    if not expected:
        return None
    script_b64 = base64.b64encode(_REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
    return (
        f"printf %s {script_b64} | base64 -d "
        f"| python3 - {shlex.quote(bind_root)} {shlex.quote(json.dumps(expected))}"
    )
