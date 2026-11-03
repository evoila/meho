# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Helpers for the tests of build-download.sh and check-download.sh.

The tests run the shell functions of packaging/ with bash, offline, and
change nothing outside a temporary folder:

- PATH holds stub commands for regctl (the registry tool), cosign and zstd, and
  links to the real tools the functions need. Each stub answers from one
  file in the "fake" folder and writes its arguments to calls.log.
- The image archives are small OCI image layouts that the tests build in
  Python. The zstd stub passes the data through unchanged, so an archive
  named .tar.zst is a plain tar here.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LINUX_SERVER = Path(__file__).resolve().parents[1]
PACKAGING = LINUX_SERVER / "packaging"
REPO_ROOT = LINUX_SERVER.parents[1]
FAIL_LINE = re.compile(r"^\[FAIL\] .*$", re.MULTILINE)

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"

REAL_TOOLS = (
    "bash",
    "env",
    "cat",
    "tar",
    "jq",
    "sha256sum",
    "awk",
    "sed",
    "grep",
    "sort",
    "comm",
    "paste",
    "uniq",
    "wc",
    "mktemp",
    "rm",
    "mkdir",
    "mv",
    "stat",
    "dirname",
    "find",
    "install",
    "chmod",
    "head",
    "date",
)

STUBS = {
    "regctl": textwrap.dedent("""\
        case "$1 $2" in
          "image digest")
            awk -v ref="$3" '$1 == ref { print $2; found = 1 } END { exit !found }' \\
                "$MEHO_FAKE/regctl_digests" || { echo "regctl: $3 not found" >&2; exit 1; } ;;
          "registry login")
            cat > "$MEHO_FAKE/regctl_login_stdin"
            exit "$(cat "$MEHO_FAKE/regctl_login_rc")" ;;
          *) exit 1 ;;
        esac
        """),
    # cosign verify passes only for a line "<reference> <identity> <issuer>"
    # of cosign_signers. cosign login keeps what it read on stdin.
    "cosign": textwrap.dedent("""\
        case "$1" in
          verify)
            reference=$2; identity=""; issuer=""; shift 2
            while [ $# -gt 0 ]; do
              case "$1" in
                --certificate-identity) identity=$2; shift 2 ;;
                --certificate-oidc-issuer) issuer=$2; shift 2 ;;
                *) shift ;;
              esac
            done
            grep -qxF "$reference $identity $issuer" "$MEHO_FAKE/cosign_signers" ||
                { echo "cosign: none of the signatures matched $identity" >&2; exit 1; } ;;
          login) cat > "$MEHO_FAKE/cosign_login_stdin" ;;
          *) exit 1 ;;
        esac
        """),
    "zstd": textwrap.dedent("""\
        for last in "$@"; do :; done
        case " $* " in
          *" -dc "*)
            if [ "$(cat "$MEHO_FAKE/zstd_rc")" != 0 ]; then
              echo "zstd: Frame requires too much memory for decoding" >&2
              exit 1
            fi
            cat "$last" ;;
          *) exit 1 ;;
        esac
        """),
}

DEFAULT_ANSWERS = {
    "regctl_digests": "",
    "cosign_signers": "",
    "regctl_login_rc": "0",
    "zstd_rc": "0",
}


def _write_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = f'printf \'%s %s\\n\' "{path.name}" "$*" >> "$MEHO_FAKE/calls.log"\n'
    path.write_text("#!/bin/sh\n" + log + body)
    path.chmod(0o755)


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class PackagingShell:
    """Runs the functions of packaging/*.sh against stubs, in a temporary folder."""

    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="meho-download-test-")
        self.base = Path(self._tempdir.name)
        self.stubs = self.base / "stubs"
        self.tools = self.base / "tools"
        self.fake = self.base / "fake"
        self.work = self.base / "work"
        for folder in (self.stubs, self.tools, self.fake, self.work):
            folder.mkdir()
        for tool in REAL_TOOLS:
            found = shutil.which(tool)
            if found is None:
                raise RuntimeError(f"the tests need '{tool}' on PATH")
            (self.tools / tool).symlink_to(found)
        for name, body in STUBS.items():
            _write_script(self.stubs / name, body)
        for name, content in DEFAULT_ANSWERS.items():
            self.answer(name, content)
        (self.fake / "calls.log").write_text("")

    def close(self) -> None:
        self._tempdir.cleanup()

    def answer(self, name: str, content: str) -> None:
        """Set what one stub answers. The names are in DEFAULT_ANSWERS."""
        (self.fake / name).write_text(content)

    def calls(self) -> str:
        return (self.fake / "calls.log").read_text()

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.stubs}:{self.tools}",
            "LINUX_SERVER_DIR": str(LINUX_SERVER),
            "PACKAGING_DIR": str(PACKAGING),
            "REPO_ROOT": str(REPO_ROOT),
            "MEHO_FAKE": str(self.fake),
            "HOME": str(self.base),
            "LC_ALL": "C",
        }
        env.update(extra or {})
        return env

    def run(
        self, function: str, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run one function of packaging/*.sh with the arguments."""
        return self.run_snippet('"$@"', function, *args, env=env)

    def run_snippet(
        self, code: str, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run bash code after packaging/*.sh is sourced; the arguments are $1..."""
        script = (
            "set -Eeuo pipefail\n"
            'source "$PACKAGING_DIR/common.sh"\n'
            'source "$PACKAGING_DIR/images.sh"\n'
            'source "$PACKAGING_DIR/parts.sh"\n'
            f"{code}\n"
        )
        return subprocess.run(
            [str(self.tools / "bash"), "-c", script, "bash", *args],
            capture_output=True,
            text=True,
            env=self.environment(env),
            cwd=self.work,
            check=False,
            timeout=60,
        )

    def run_script(
        self, script: str, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run build-download.sh or check-download.sh with the arguments."""
        return subprocess.run(
            [str(LINUX_SERVER / script), *args],
            capture_output=True,
            text=True,
            env=self.environment(env),
            cwd=self.work,
            check=False,
            timeout=60,
        )


@dataclass
class FakeLayout:
    """An OCI image layout built in memory: blobs by digest, and index entries."""

    blobs: dict[str, bytes] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)

    def add_blob(self, data: bytes, media_type: str) -> dict[str, Any]:
        digest = digest_of(data)
        self.blobs[digest] = data
        return {"mediaType": media_type, "digest": digest, "size": len(data)}

    def add_image(self, name: str, *, architecture: str = "amd64", index: bool = True) -> str:
        """Add one image and name it. With index=True the image is a sparse
        index of two platforms: linux/amd64 is complete, linux/arm64 is only
        listed, as in an image fetched for one platform. Returns its digest."""
        layer = self.add_blob(f"layer of {name}".encode(), OCI_LAYER)
        config_data = {"os": "linux", "architecture": architecture, "rootfs": {"type": "layers"}}
        config = self.add_blob(json.dumps(config_data).encode(), OCI_CONFIG)
        manifest_data = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": config,
            "layers": [layer],
        }
        manifest = self.add_blob(json.dumps(manifest_data).encode(), OCI_MANIFEST)
        top = manifest
        if index:
            other = json.dumps({"schemaVersion": 2, "note": f"arm64 of {name}"}).encode()
            index_data = {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": [
                    {**manifest, "platform": {"os": "linux", "architecture": architecture}},
                    {
                        "mediaType": OCI_MANIFEST,
                        "digest": digest_of(other),
                        "size": len(other),
                        "platform": {"os": "linux", "architecture": "arm64"},
                    },
                ],
            }
            top = self.add_blob(json.dumps(index_data).encode(), OCI_INDEX)
        self.name(name, top)
        return str(top["digest"])

    def name(self, name: str, descriptor: dict[str, Any]) -> None:
        annotations = {
            "io.containerd.image.name": name,
            "org.opencontainers.image.ref.name": name.rsplit(":", 1)[-1],
        }
        self.entries.append({**descriptor, "annotations": annotations})

    def manifest_of(self, top_digest: str) -> dict[str, Any]:
        """The linux/amd64 manifest of an image added with add_image."""
        top = json.loads(self.blobs[top_digest])
        if top.get("mediaType") == OCI_INDEX:
            top = json.loads(self.blobs[top["manifests"][0]["digest"]])
        return dict(top)

    def write_tar(self, path: Path, *, oci_layout: bool = True) -> None:
        index = {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": self.entries}
        files = {"index.json": json.dumps(index).encode()}
        if oci_layout:
            files["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
        for digest, data in self.blobs.items():
            files["blobs/sha256/" + digest.removeprefix("sha256:")] = data
        write_tar(path, files)


def write_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


class PackagingTestCase(unittest.TestCase):
    """Gives each test its own shell in self.shell."""

    def setUp(self) -> None:
        self.shell = PackagingShell()
        self.addCleanup(self.shell.close)

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_succeeds(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(0, result.returncode, self.output(result))
        self.assertEqual([], FAIL_LINE.findall(self.output(result)))

    def assert_fails_once(self, result: subprocess.CompletedProcess[str], *fragments: str) -> str:
        """Exit code 1, exactly one [FAIL] line, and the line says what to fix."""
        output = self.output(result)
        self.assertEqual(1, result.returncode, output)
        fail_lines: list[str] = FAIL_LINE.findall(output)
        self.assertEqual(1, len(fail_lines), output)
        line = fail_lines[0]
        self.assertIn(" Fix: ", line)
        for fragment in fragments:
            self.assertIn(fragment, line)
        return line

    def assert_usage_error(self, result: subprocess.CompletedProcess[str], fragment: str) -> None:
        self.assertEqual(2, result.returncode, self.output(result))
        self.assertIn(fragment, result.stderr)
