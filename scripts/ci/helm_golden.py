#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render the Helm chart with fixed values and compare it with the golden files.

The golden files in ``deploy/golden/`` hold what ``helm template`` makes of
``deploy/charts/meho/`` for a few fixed sets of values. A change to a
template or to a default shows up as a change in these files, so a reviewer
sees the effect on the installed objects. A check fails when a golden file
is not equal to the render.

Three values in a render are random on every run (two passwords and the
checksum that follows one). They are replaced with ``<random>``.

Usage::

    python scripts/ci/helm_golden.py            # check, exit 1 on a difference
    python scripts/ci/helm_golden.py --write    # make the golden files again

CI: the job ``validate`` in ``.github/workflows/chart.yml``. Needs ``helm``.
Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import re
import subprocess
import sys

CHART = "deploy/charts/meho"
GOLDEN_DIR = "deploy/golden"

# Test values that satisfy the chart's schema. Not real hosts or secrets.
_COMMON = [
    "--namespace", "meho",
    "--set", "image.tag=test",
    "--set", "ingress.host=meho.test",
]  # fmt: skip
_BRING_YOUR_OWN = [
    "--set", "ingress.tls.secretName=meho-tls",
    "--set", "corePostgres.enabled=false",
    "--set", "coreKeycloak.enabled=false",
    "--set", "coreVault.enabled=false",
    "--set", "postgres.credentialsSecret=meho-postgres",
    "--set", "vault.address=https://vault.test",
    "--set", "keycloak.issuer=https://keycloak.test/realms/meho",
    "--set", "config.keycloakIssuerUrl=https://keycloak.test/realms/meho",
    "--set", "config.keycloakAudience=meho-backplane",
    "--set", "config.vaultAddr=https://vault.test",
    "--set", "config.backplaneUrl=https://meho.test",
    "--set", "networkPolicy.postgresCIDR=10.0.1.0/24",
    "--set", "networkPolicy.vaultCIDR=10.0.2.0/24",
    "--set", "networkPolicy.keycloakCIDR=10.0.3.0/24",
]  # fmt: skip
_BUILT_IN = [
    "--set", "coreKeycloak.hostname=meho.test",
    "--set", "coreKeycloak.firstTenantId=00000000-0000-4000-8000-000000000001",
]  # fmt: skip

# name -> extra helm arguments (the golden file is deploy/golden/<name>.yaml)
SCENARIOS: dict[str, list[str]] = {
    "built-in-core": _BUILT_IN,
    "bring-your-own": _BRING_YOUR_OWN,
    "openshift": ["-f", "deploy/values-examples/values-openshift.yaml", *_BRING_YOUR_OWN],
}

_RANDOM_LINE = re.compile(r"^(\s+(?:requirepass|token): )\".*\"$")
_RANDOM_CHECKSUM = re.compile(r"^(\s+checksum/metrics-auth: ).*$")


def mask_random(text: str) -> str:
    """Replace the values that change on every render and trim trailing spaces."""
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        line = _RANDOM_LINE.sub(r'\1"<random>"', line)
        line = _RANDOM_CHECKSUM.sub(r"\1<random>", line)
        out.append(line)
    return "\n".join(out) + "\n"


def render(root: pathlib.Path, name: str) -> str:
    """Return the masked ``helm template`` output of scenario *name*."""
    result = subprocess.run(
        ["helm", "template", "golden", CHART, *_COMMON, *SCENARIOS[name]],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed for {name!r}: {result.stderr.strip()}")
    return mask_random(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--write", action="store_true", help="make the golden files again")
    args = parser.parse_args(argv)
    root = pathlib.Path(args.root).resolve()
    golden = root / GOLDEN_DIR

    failed = False
    for name in SCENARIOS:
        try:
            current = render(root, name)
        except (RuntimeError, OSError) as exc:
            print(f"::error title=Helm render failed::{exc}")
            return 2
        path = golden / f"{name}.yaml"
        if args.write:
            golden.mkdir(parents=True, exist_ok=True)
            path.write_text(current, encoding="utf-8")
            print(f"wrote {path.relative_to(root)}")
            continue
        expected = path.read_text(encoding="utf-8") if path.exists() else ""
        if expected == current:
            print(f"ok: {name}")
            continue
        failed = True
        diff = difflib.unified_diff(
            expected.splitlines(),
            current.splitlines(),
            f"{GOLDEN_DIR}/{name}.yaml (committed)",
            f"{name} (rendered now)",
            lineterm="",
        )
        print("\n".join(list(diff)[:200]))
    if failed:
        print(
            "::error title=Helm golden files out of date::The chart renders differently from "
            f"{GOLDEN_DIR}/. Run 'python scripts/ci/helm_golden.py --write' and commit the files."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
