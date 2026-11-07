# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``scripts/release/security_sheet.py``.

The script writes the security sheet of a release from the data the
release job collects. The tests run it as a subprocess against a data
folder in ``tmp_path``, with the real template from ``docs/security/``.
They check the normal case, every input that must stop the script, and
that the sheet never reads as "nothing found" when the data is broken.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


_SCRIPT = _repo_root() / "scripts" / "release" / "security_sheet.py"
_TEMPLATE = _repo_root() / "docs" / "security" / "security-sheet-template.md"

_IMAGE_DIGEST = "sha256:" + "a" * 64
_CHART_DIGEST = "sha256:" + "b" * 64

_FACTS = {
    "tag": "v1.0.0",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "image_digest": _IMAGE_DIGEST,
    "chart_digest": _CHART_DIGEST,
    "made_on": "2026-11-07",
}

_PORTS = "80/tcp open\n443/tcp open\n22/tcp admin-network\n6443/tcp closed\n8472/udp closed\n"


def _vuln(vid: str, severity: str, fixed: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "VulnerabilityID": vid,
        "PkgName": "libexample",
        "InstalledVersion": "1.0",
        "Severity": severity,
    }
    if fixed is not None:
        item["FixedVersion"] = fixed
    return item


def _trivy(*vulns: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = [
        {"Target": "ghcr.io/evoila/meho (debian 12.12)", "Class": "os-pkgs", "Type": "debian"},
        {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg"},
    ]
    if vulns:
        results[0]["Vulnerabilities"] = list(vulns)
    return {
        "SchemaVersion": 2,
        "ArtifactName": "ghcr.io/evoila/meho@" + _IMAGE_DIGEST,
        "Results": results,
    }


_SBOM = {
    "spdxVersion": "SPDX-2.3",
    "creationInfo": {"creators": ["Organization: Anchore, Inc", "Tool: syft-1.54.0"]},
    "packages": [{"name": "python"}, {"name": "openssl"}, {"name": "fastapi"}],
}

_TRIVYIGNORE = "# Dated exceptions.\n# <finding id> exp:<YYYY-MM-DD>\n"

_CIS_EXCEPTIONS = """# CIS exceptions of the MEHO appliance

Some text.

| Scanner rule | CIS | Fails on | Why it fails |
| --- | --- | --- | --- |
| `grub2_password` | 1.4.1 | the build | Set at first start. |
"""


def _data(tmp_path: Path, **override: Any) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    files: dict[str, Any] = {
        "facts.json": _FACTS,
        "trivy.json": _trivy(),
        "sbom.spdx.json": _SBOM,
        "ports.txt": _PORTS,
        "trivyignore": _TRIVYIGNORE,
        "cis-exceptions.md": _CIS_EXCEPTIONS,
    }
    names = {
        "facts_json": "facts.json",
        "trivy_json": "trivy.json",
        "sbom_spdx_json": "sbom.spdx.json",
        "ports_txt": "ports.txt",
        "trivyignore": "trivyignore",
        "cis_exceptions": "cis-exceptions.md",
    }
    files.update({names[key]: value for key, value in override.items()})
    for name, value in files.items():
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        (data / name).write_text(text, encoding="utf-8")
    return data


def _run(data: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], str | None]:
    out = data.parent / "sheet.md"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--data", str(data), "--out", str(out), *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, out.read_text(encoding="utf-8") if out.exists() else None


def test_writes_every_measured_part(tmp_path: Path) -> None:
    trivy = _trivy(
        _vuln("CVE-2026-0001", "HIGH"),
        _vuln("CVE-2026-0002", "MEDIUM", fixed="1.1"),
        _vuln("CVE-2026-0003", "CRITICAL"),
        _vuln("CVE-2026-0004", "LOW"),
    )
    proc, sheet = _run(_data(tmp_path, trivy_json=trivy))
    assert proc.returncode == 0, proc.stderr
    assert sheet is not None
    assert "{{" not in sheet
    assert sheet.startswith("# MEHO 1.0.0: security sheet")
    assert f"ghcr.io/evoila/meho@{_IMAGE_DIGEST}" in sheet
    assert f"ghcr.io/evoila/meho-chart@{_CHART_DIGEST}" in sheet
    assert "image.yml@refs/tags/v1.0.0" in sheet
    assert "| 22/tcp | admin-network |" in sheet
    assert "| 8472/udp | closed |" in sheet
    assert "| CRITICAL | 0 | 1 |" in sheet
    assert "| HIGH | 0 | 1 |" in sheet
    assert "| MEDIUM | 1 | 0 |" in sheet
    # CRITICAL first, then HIGH; MEDIUM and LOW are only counted.
    assert sheet.index("CVE-2026-0003") < sheet.index("CVE-2026-0001")
    assert "CVE-2026-0002" not in sheet
    assert "- Packages listed: 3" in sheet
    assert "- Made by: syft-1.54.0" in sheet
    assert "`meho-sbom-v1.0.0.spdx.json`" in sheet
    assert "`meho-vulnerability-scan-v1.0.0.json`" in sheet
    assert "| `grub2_password` | 1.4.1 | the build | Set at first start. |" in sheet
    assert "blob/v1.0.0/deploy/appliance/cis/not-applicable.md" in sheet
    # No lab folder: the three lab parts say so, they are not left empty.
    assert sheet.count("Not measured for this release yet.") == 3


def test_clean_scan_says_none(tmp_path: Path) -> None:
    proc, sheet = _run(_data(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert sheet is not None
    assert "| CRITICAL | 0 | 0 |" in sheet
    assert "**CRITICAL and HIGH findings:**\n\nNone." in sheet
    assert "(the scan exception list):**\n\nNone." in sheet


def test_lists_active_scan_exceptions(tmp_path: Path) -> None:
    ignore = _TRIVYIGNORE + "CVE-2026-0009 exp:2026-12-31\n"
    proc, sheet = _run(_data(tmp_path, trivyignore=ignore))
    assert proc.returncode == 0, proc.stderr
    assert sheet is not None
    assert "- `CVE-2026-0009 exp:2026-12-31`" in sheet


def test_lab_results_are_included_without_addresses(tmp_path: Path) -> None:
    data = _data(tmp_path)
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "ports.txt").write_text(
        "Nmap scan report for 192.0.2.10\nPORT    STATE SERVICE\n443/tcp open  https\n",
        encoding="utf-8",
    )
    (lab / "rke2-cis.txt").write_text("```\nall checks passed\n", encoding="utf-8")
    (lab / "ubuntu-cis.txt").write_text(
        "# CIS Ubuntu Linux 24.04 LTS Benchmark v1.0.0, Level 1 - Server\n"
        "pass sshd_disable_root_login\n"
        "pass package_aide_installed\n"
        "notapplicable sysctl_net_ipv4_ip_forward\n"
        "fail grub2_password\n",
        encoding="utf-8",
    )
    proc, sheet = _run(data, "--lab", str(lab))
    assert proc.returncode == 0, proc.stderr
    assert sheet is not None
    assert "192.0.2.10" not in sheet
    assert "Nmap scan report for <address>" in sheet
    # A fence in the text gets a longer fence around it.
    assert "````text\n```\nall checks passed\n````" in sheet
    assert "| pass | 2 |" in sheet
    assert "| notapplicable | 1 |" in sheet
    assert "| `grub2_password` | fail |" in sheet
    assert "sshd_disable_root_login" not in sheet
    assert "Not measured for this release yet." not in sheet


def test_empty_lab_files_count_as_not_measured(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "ports.txt").write_text("\n\n", encoding="utf-8")
    (lab / "ubuntu-cis.txt").write_text("pass a_rule\n", encoding="utf-8")
    proc, sheet = _run(_data(tmp_path), "--lab", str(lab))
    assert proc.returncode == 0, proc.stderr
    assert sheet is not None
    # ports.txt is empty and rke2-cis.txt is missing.
    assert sheet.count("Not measured for this release yet.") == 2
    assert "Rules that do not pass:\n\nNone." in sheet


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# only a comment\n", "lists no rule"),
        ("pass\n", "line 1"),
        ("pass a_rule extra\n", "line 1"),
    ],
)
def test_broken_ubuntu_cis_result_stops(tmp_path: Path, text: str, message: str) -> None:
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "ubuntu-cis.txt").write_text(text, encoding="utf-8")
    proc, sheet = _run(_data(tmp_path), "--lab", str(lab))
    assert proc.returncode == 2
    assert message in proc.stderr
    assert sheet is None


def test_empty_exception_list_says_none(tmp_path: Path) -> None:
    proc, sheet = _run(
        _data(tmp_path, cis_exceptions="# CIS exceptions\n\n| Rule | Why |\n| --- | --- |\n")
    )
    assert proc.returncode == 0, proc.stderr
    assert sheet is not None
    assert "approved once per release):**\n\nNone." in sheet


def test_missing_lab_folder_fails(tmp_path: Path) -> None:
    proc, sheet = _run(_data(tmp_path), "--lab", str(tmp_path / "nope"))
    assert proc.returncode == 2
    assert "lab folder" in proc.stderr
    assert sheet is None


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"facts_json": None}, "cannot read"),
        ({"facts_json": "{not json"}, "not valid JSON"),
        ({"facts_json": {**_FACTS, "commit": ""}}, "no value for 'commit'"),
        ({"facts_json": {**_FACTS, "tag": "1.0.0"}}, "is not like v1.0.0"),
        ({"facts_json": {**_FACTS, "image_digest": "sha256:short"}}, "not a sha256 digest"),
        ({"trivy_json": None}, "cannot read"),
        ({"trivy_json": {"SchemaVersion": 2}}, "no scanned target"),
        ({"trivy_json": {"SchemaVersion": 2, "Results": []}}, "no scanned target"),
        ({"trivy_json": {"Results": [{"Target": "x"}]}}, "SchemaVersion 2"),
        (
            {
                "trivy_json": {
                    "SchemaVersion": 2,
                    "Results": [{"Target": "x", "Vulnerabilities": {}}],
                }
            },
            "not a list",
        ),
        (
            {
                "trivy_json": {
                    "SchemaVersion": 2,
                    "Results": [{"Target": "x", "Vulnerabilities": [{}]}],
                }
            },
            "without a VulnerabilityID",
        ),
        ({"sbom_spdx_json": {"packages": [{}]}}, "no 'spdxVersion'"),
        ({"sbom_spdx_json": {"spdxVersion": "SPDX-2.3", "packages": []}}, "lists no package"),
        ({"ports_txt": ""}, "lists no port"),
        ({"ports_txt": "443/tcp maybe\n"}, "line 1"),
        ({"trivyignore": None}, "cannot read"),
        ({"cis_exceptions": None}, "cannot read"),
    ],
)
def test_bad_data_stops_without_a_sheet(
    tmp_path: Path, override: dict[str, Any], message: str
) -> None:
    proc, sheet = _run(_data(tmp_path, **override))
    assert proc.returncode == 2
    assert message in proc.stderr
    assert sheet is None


def test_sbom_without_tool_name(tmp_path: Path) -> None:
    sbom = {"spdxVersion": "SPDX-2.3", "creationInfo": "odd", "packages": [{"name": "x"}]}
    proc, sheet = _run(_data(tmp_path, sbom_spdx_json=sbom))
    assert proc.returncode == 0, proc.stderr
    assert sheet is not None
    assert "- Made by: not named in the SBOM" in sheet


def test_template_and_script_agree(tmp_path: Path) -> None:
    """A placeholder without a value, or a value without a placeholder, fails."""
    data = _data(tmp_path)
    broken = tmp_path / "template.md"
    broken.write_text(
        _TEMPLATE.read_text(encoding="utf-8") + "\n{{ unknown_part }}\n", encoding="utf-8"
    )
    proc, _ = _run(data, "--template", str(broken))
    assert proc.returncode == 2
    assert "unknown_part" in proc.stderr

    short = tmp_path / "short.md"
    short.write_text("# {{ version }}\n", encoding="utf-8")
    proc, _ = _run(data, "--template", str(short))
    assert proc.returncode == 2
    assert "does not use" in proc.stderr
