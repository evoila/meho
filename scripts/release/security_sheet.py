#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the security sheet of a release from the measured data.

The security sheet is one short Markdown file for the customer's security
team. The release job (``.github/workflows/security-sheet.yml``) collects
the data of a release tag into one folder, runs this script and attaches
the sheet to the GitHub Release. The template is
``docs/security/security-sheet-template.md``.

The data folder (``--data``) holds:

* ``facts.json``: ``{"tag", "commit", "image_digest", "chart_digest",
  "made_on"}``;
* ``trivy.json``: the Trivy JSON report of the MEHO image;
* ``sbom.spdx.json``: the SBOM of the MEHO image (SPDX JSON);
* ``ports.txt``: the output of ``firewall_port_list`` of the tag;
* ``trivyignore``: the ``.trivyignore`` file of the tag;
* ``cis-exceptions.md``: ``deploy/appliance/cis/exceptions.md`` of the tag.

The lab folder (``--lab``, optional) holds the results of a test install,
as plain text: ``ports.txt`` (a port scan) and ``rke2-cis.txt``, shown as
they are, and ``ubuntu-cis.txt``, the result list of the appliance's CIS
scan (``meho-appliance-<version>-cis.txt``, one ``<result> <rule>`` line
per rule), shown as counts and the rules that do not pass. A missing file
gives the line "Not measured for this release yet." IPv4 addresses in the
shown text are replaced with ``<address>``, because the sheet is public.

A file that is missing or has the wrong shape stops the script with exit
code 2. The script never writes a sheet that reads as "nothing found"
when it could not read the data.

Usage::

    python3 scripts/release/security_sheet.py --data DIR [--lab DIR] --out FILE
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "docs" / "security" / "security-sheet-template.md"

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
PORT_STATES = {"open", "admin-network", "closed"}
FACT_KEYS = ("tag", "commit", "image_digest", "chart_digest", "made_on")
LAB_TEXT_FILES = {"lab_ports": "ports.txt", "lab_rke2_cis": "rke2-cis.txt"}
LAB_UBUNTU_CIS = "ubuntu-cis.txt"
# The scan results that count as "no problem"; harden.sh uses the same set.
CIS_OK_RESULTS = ("pass", "notapplicable", "notselected", "informational")
NOT_MEASURED = "Not measured for this release yet."
VULN_REPORT_FILE = "meho-vulnerability-scan-{tag}.json"
SBOM_FILE = "meho-sbom-{tag}.spdx.json"

_PLACEHOLDER = re.compile(r"\{\{ ([a-z0-9_]+) \}\}")
_TAG = re.compile(r"^v\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


class InputError(Exception):
    """A data file is missing or has the wrong shape; the script exits 2."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not valid JSON: {exc}") from exc


def read_facts(path: Path) -> dict[str, str]:
    """The release facts. Every key must be a non-empty string."""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise InputError(f"{path} must hold a JSON object.")
    facts: dict[str, str] = {}
    for key in FACT_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"{path} has no value for '{key}'.")
        facts[key] = value.strip()
    if not _TAG.match(facts["tag"]):
        raise InputError(f"the tag '{facts['tag']}' is not like v1.0.0.")
    for key in ("image_digest", "chart_digest"):
        if not _DIGEST.match(facts[key]):
            raise InputError(f"'{key}' is not a sha256 digest: {facts[key]}")
    return facts


def port_rows(text: str) -> str:
    """Table rows from ``firewall_port_list`` lines: ``<port>/<protocol> <who>``."""
    rows: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or "/" not in parts[0] or parts[1] not in PORT_STATES:
            raise InputError(f"ports.txt line {number} is not '<port>/<protocol> <who>': {line!r}")
        rows.append(f"| {parts[0]} | {parts[1]} |")
    if not rows:
        raise InputError("ports.txt lists no port.")
    return "\n".join(rows)


def _vulnerabilities(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        raise InputError("trivy.json is not a Trivy JSON report (SchemaVersion 2).")
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise InputError("trivy.json has no scanned target, so the scan did not read the image.")
    found: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise InputError("trivy.json has a result that is not an object.")
        # Trivy leaves "Vulnerabilities" out when a target has none.
        items = result.get("Vulnerabilities", [])
        if not isinstance(items, list):
            raise InputError("trivy.json has a 'Vulnerabilities' value that is not a list.")
        for item in items:
            if not isinstance(item, dict) or not item.get("VulnerabilityID"):
                raise InputError("trivy.json has a finding without a VulnerabilityID.")
            found.append(item)
    return found


def _severity(item: dict[str, Any]) -> str:
    value = str(item.get("Severity") or "UNKNOWN").upper()
    return value if value in SEVERITIES else "UNKNOWN"


def vuln_parts(report: Any) -> tuple[str, str]:
    """The count table and the list of CRITICAL and HIGH findings."""
    found = _vulnerabilities(report)
    with_fix: Counter[str] = Counter()
    without_fix: Counter[str] = Counter()
    for item in found:
        (with_fix if item.get("FixedVersion") else without_fix)[_severity(item)] += 1
    lines = [
        "| Severity | With a fix | No fix yet |",
        "|---|---|---|",
        *(f"| {sev} | {with_fix[sev]} | {without_fix[sev]} |" for sev in SEVERITIES),
    ]
    serious = sorted(
        (item for item in found if _severity(item) in ("CRITICAL", "HIGH")),
        key=lambda item: (SEVERITIES.index(_severity(item)), str(item["VulnerabilityID"])),
    )
    if not serious:
        return "\n".join(lines), "None."
    rows = [
        "| Finding | Severity | Package | Installed | Fixed in |",
        "|---|---|---|---|---|",
    ]
    for item in serious:
        cells = (
            item["VulnerabilityID"],
            _severity(item),
            item.get("PkgName") or "?",
            item.get("InstalledVersion") or "?",
            item.get("FixedVersion") or "no fix yet",
        )
        rows.append("| " + " | ".join(_cell(str(cell)) for cell in cells) + " |")
    return "\n".join(lines), "\n".join(rows)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def vuln_exceptions(trivyignore: str) -> str:
    """The active lines of ``.trivyignore``, or "None."."""
    entries = [
        line.strip()
        for line in trivyignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not entries:
        return "None."
    return "\n".join(f"- `{entry}`" for entry in entries)


def sbom_parts(sbom: Any) -> dict[str, str]:
    """Format, package count and tool of an SPDX JSON SBOM."""
    if not isinstance(sbom, dict):
        raise InputError("sbom.spdx.json must hold a JSON object.")
    version = sbom.get("spdxVersion")
    packages = sbom.get("packages")
    if not isinstance(version, str) or not version.startswith("SPDX-"):
        raise InputError("sbom.spdx.json has no 'spdxVersion', so it is not an SPDX JSON SBOM.")
    if not isinstance(packages, list) or not packages:
        raise InputError("sbom.spdx.json lists no package.")
    info = sbom.get("creationInfo")
    creators = info.get("creators") if isinstance(info, dict) else None
    if not isinstance(creators, list):
        creators = []
    tools = [
        c.removeprefix("Tool: ") for c in creators if isinstance(c, str) and c.startswith("Tool: ")
    ]
    return {
        "sbom_format": f"{version} (JSON)",
        "sbom_package_count": str(len(packages)),
        "sbom_tool": ", ".join(tools) or "not named in the SBOM",
    }


def lab_block(path: Path) -> str:
    """The text of a lab result file in a code block, or "Not measured"."""
    if not path.is_file():
        return NOT_MEASURED
    text = _IPV4.sub("<address>", _read_text(path)).strip("\n")
    if not text.strip():
        return NOT_MEASURED
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def ubuntu_cis_block(path: Path) -> str:
    """Counts per result and the rules that do not pass, from the scan's result list."""
    if not path.is_file():
        return NOT_MEASURED
    counts: Counter[str] = Counter()
    failing: list[str] = []
    for number, line in enumerate(_read_text(path).splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise InputError(f"ubuntu-cis.txt line {number} is not '<result> <rule>': {line!r}")
        result, rule = parts
        counts[result] += 1
        if result not in CIS_OK_RESULTS:
            failing.append(f"| `{_cell(rule)}` | {_cell(result)} |")
    if not counts:
        raise InputError("ubuntu-cis.txt lists no rule, so it is not a scan result.")
    lines = ["| Result | Rules |", "|---|---|"]
    lines += [f"| {_cell(result)} | {counts[result]} |" for result in sorted(counts)]
    lines += ["", "Rules that do not pass:", ""]
    if failing:
        lines += ["| Rule | Result |", "|---|---|", *failing]
    else:
        lines.append("None.")
    return _IPV4.sub("<address>", "\n".join(lines))


def cis_exceptions(text: str) -> str:
    """The table of ``exceptions.md``: its heading row and one row per rule."""
    rows = [line.strip() for line in text.splitlines() if line.lstrip().startswith("|")]
    if len(rows) <= 2:
        return "None."
    return "\n".join(rows)


def render(template: str, values: dict[str, str]) -> str:
    """Fill every ``{{ name }}`` of the template. Every value must be used."""
    used: set[str] = set()

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise InputError(
                f"the template names '{{{{ {name} }}}}', but there is no value for it."
            )
        used.add(name)
        return values[name]

    text = _PLACEHOLDER.sub(fill, template)
    unused = sorted(set(values) - used)
    if unused:
        raise InputError(f"the template does not use: {', '.join(unused)}.")
    return text


def sheet(data: Path, lab: Path | None, template: str) -> str:
    """The security sheet for the data folder and the optional lab folder."""
    facts = read_facts(data / "facts.json")
    tag = facts["tag"]
    counts, findings = vuln_parts(_read_json(data / "trivy.json"))
    values = {
        **facts,
        "version": tag.removeprefix("v"),
        "port_rows": port_rows(_read_text(data / "ports.txt")),
        "vuln_counts": counts,
        "vuln_findings": findings,
        "vuln_exceptions": vuln_exceptions(_read_text(data / "trivyignore")),
        "cis_exceptions": cis_exceptions(_read_text(data / "cis-exceptions.md")),
        "vuln_report_file": VULN_REPORT_FILE.format(tag=tag),
        "sbom_file": SBOM_FILE.format(tag=tag),
        **sbom_parts(_read_json(data / "sbom.spdx.json")),
    }
    for key, name in LAB_TEXT_FILES.items():
        values[key] = lab_block(lab / name) if lab is not None else NOT_MEASURED
    values["lab_ubuntu_cis"] = (
        ubuntu_cis_block(lab / LAB_UBUNTU_CIS) if lab is not None else NOT_MEASURED
    )
    return render(template, values)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--data", type=Path, required=True, help="the data folder of the release")
    parser.add_argument("--lab", type=Path, help="the folder with the results of a test install")
    parser.add_argument("--template", type=Path, default=TEMPLATE, help="the sheet template")
    parser.add_argument("--out", type=Path, required=True, help="where to write the sheet")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.lab is not None and not args.lab.is_dir():
            raise InputError(f"the lab folder {args.lab} does not exist.")
        text = sheet(args.data, args.lab, _read_text(args.template))
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(text, encoding="utf-8")
    print(f"security sheet: wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
