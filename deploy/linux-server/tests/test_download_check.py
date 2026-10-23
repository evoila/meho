#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of check-download.sh that run offline: the layout of the download
and the checks of the image archive."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import unittest
from pathlib import Path

from download_harness import LINUX_SERVER, FakeLayout, PackagingTestCase, write_tar

TOP = "meho-linux-0.36.0-amd64"
EXECUTABLES = {"install.sh", "rke2/install.sh", "bin/helm", "bin/meho"}


class LayoutTests(PackagingTestCase):
    def expected_files(self) -> list[str]:
        result = self.shell.run("expected_layout", "0.36.0")
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.split()

    def write_download(
        self,
        files: list[str],
        *,
        owner: int = 0,
        modes: dict[str, int] | None = None,
        links: tuple[str, ...] = (),
    ) -> str:
        path = self.shell.work / f"{TOP}.tar"
        with tarfile.open(path, "w") as archive:
            folder = tarfile.TarInfo(TOP)
            folder.type = tarfile.DIRTYPE
            folder.mode = 0o755
            archive.addfile(folder)
            for name in files:
                info = tarfile.TarInfo(f"{TOP}/{name}")
                info.uid = info.gid = owner
                info.mode = (modes or {}).get(name, 0o755 if name in EXECUTABLES else 0o644)
                data = f"content of {name}\n".encode()
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            for name in links:
                link = tarfile.TarInfo(f"{TOP}/{name}")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                archive.addfile(link)
        return str(path)

    def check(self, path: str) -> subprocess.CompletedProcess[str]:
        return self.shell.run("check_layout", path, TOP, "0.36.0")

    def test_the_layout_is_the_one_of_the_design(self) -> None:
        files = self.expected_files()

        self.assertIn("chart/meho-0.36.0.tgz", files)
        self.assertIn("images/meho-images.linux-amd64.tar.zst", files)
        self.assertIn("LICENSES/README.txt", files)
        self.assertIn("lib/common.sh", files)
        self.assertIn("lib/firewall.sh", files)
        self.assertEqual(len(files), len(set(files)))

    def test_the_layout_holds_every_file_install_sh_reads(self) -> None:
        result = self.shell.run_snippet(
            'source "$LINUX_SERVER_DIR/lib/common.sh"; printf "%s\\n" "${DOWNLOAD_FILES[@]}"'
        )
        self.assertEqual(0, result.returncode, result.stderr)

        self.assertLessEqual(set(result.stdout.split()), set(self.expected_files()))
        self.assertIn("install.sh", self.expected_files())

    def test_a_complete_download_passes(self) -> None:
        result = self.check(self.write_download(self.expected_files()))

        self.assert_succeeds(result)
        self.assertIn("[ok]", result.stdout)

    def test_a_missing_file_fails_and_is_named(self) -> None:
        files = [name for name in self.expected_files() if name != "bin/meho"]

        self.assert_fails_once(self.check(self.write_download(files)), f"Missing: {TOP}/bin/meho.")

    def test_an_extra_file_fails_and_is_named(self) -> None:
        files = [*self.expected_files(), "notes.txt"]

        self.assert_fails_once(
            self.check(self.write_download(files)), f"Not expected: {TOP}/notes.txt."
        )

    def test_files_of_another_owner_fail(self) -> None:
        self.assert_fails_once(
            self.check(self.write_download(self.expected_files(), owner=1001)),
            "(owner 1001/1001, not 0/0)",
        )

    def test_a_file_that_others_can_change_fails(self) -> None:
        path = self.write_download(self.expected_files(), modes={"lib/common.sh": 0o664})

        self.assert_fails_once(
            self.check(path), f"{TOP}/lib/common.sh (-rw-rw-r--, others can change it)"
        )

    def test_a_script_that_cannot_run_fails(self) -> None:
        path = self.write_download(self.expected_files(), modes={"install.sh": 0o644})

        self.assert_fails_once(self.check(path), f"{TOP}/install.sh (-rw-r--r--, not executable)")

    def test_a_link_fails(self) -> None:
        files = [name for name in self.expected_files() if name != "VERSION"]

        path = self.write_download(files, links=("VERSION",))

        self.assert_fails_once(self.check(path), f"{TOP}/VERSION (not a file or folder)")


class ImageArchiveTests(PackagingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.layout = FakeLayout()
        self.lock_lines: list[str] = []

    def add(self, name: str, *, architecture: str = "amd64", index: bool = True) -> str:
        digest = self.layout.add_image(name, architecture=architecture, index=index)
        self.lock_lines.append(f"{name} {digest}")
        return digest

    def verify(self, *, oci_layout: bool = True) -> subprocess.CompletedProcess[str]:
        archive = self.shell.work / "meho-images.linux-amd64.tar.zst"
        self.layout.write_tar(archive, oci_layout=oci_layout)
        lock = self.shell.work / "images.lock"
        lock.write_text("".join(f"{line}\n" for line in sorted(self.lock_lines)))
        return self.shell.run("verify_image_archive", str(archive), str(lock), str(self.shell.work))

    def test_an_archive_with_every_image_under_its_digest_passes(self) -> None:
        self.add("docker.io/library/busybox:1.37")
        self.add("ghcr.io/evoila/meho:v0.36.0", index=False)

        result = self.verify()

        self.assert_succeeds(result)
        self.assertIn("with 2 images, each under its pinned digest", result.stdout)

    def test_the_archive_is_read_with_the_decoder_memory_of_rke2(self) -> None:
        self.add("docker.io/library/busybox:1.37")

        self.verify()

        self.assertIn("zstd -q -dc --memory=32MiB ", self.shell.calls())

    def test_an_archive_that_needs_more_decoder_memory_fails(self) -> None:
        self.add("docker.io/library/busybox:1.37")
        self.shell.answer("zstd_rc", "1")

        self.assert_fails_once(self.verify(), "cannot be read with 32MiB of zstd decoder memory")

    def test_a_docker_save_archive_fails(self) -> None:
        self.add("docker.io/library/busybox:1.37")
        archive = self.shell.work / "meho-images.linux-amd64.tar.zst"
        manifest = [
            {"Config": "config.json", "RepoTags": ["busybox:1.37"], "Layers": ["layer.tar"]}
        ]
        write_tar(
            archive,
            {
                "manifest.json": json.dumps(manifest).encode(),
                "config.json": b"{}",
                "layer.tar": b"",
            },
        )
        lock = self.shell.work / "images.lock"
        lock.write_text("\n".join(self.lock_lines) + "\n")

        result = self.shell.run(
            "verify_image_archive", str(archive), str(lock), str(self.shell.work)
        )

        self.assert_fails_once(result, "is not an OCI image layout")

    def test_an_image_under_another_digest_fails_and_both_digests_are_named(self) -> None:
        digest = self.add("docker.io/library/busybox:1.37")
        pinned = "sha256:" + "3" * 64
        self.lock_lines = [f"docker.io/library/busybox:1.37 {pinned}"]

        line = self.assert_fails_once(self.verify())

        self.assertIn(
            f"Expected but not in the archive: docker.io/library/busybox:1.37 {pinned}.", line
        )
        self.assertIn(
            f"In the archive but not expected: docker.io/library/busybox:1.37 {digest}.", line
        )

    def test_a_missing_image_fails(self) -> None:
        self.add("docker.io/library/busybox:1.37")
        self.lock_lines.append("docker.io/valkey/valkey:9.0-alpine sha256:" + "4" * 64)

        self.assert_fails_once(
            self.verify(), "Expected but not in the archive: docker.io/valkey/valkey:9.0-alpine"
        )

    def test_an_image_that_the_chart_does_not_use_fails(self) -> None:
        self.add("docker.io/library/busybox:1.37")
        extra = self.layout.add_image("docker.io/library/alpine:3")

        self.assert_fails_once(
            self.verify(), f"In the archive but not expected: docker.io/library/alpine:3 {extra}."
        )

    def test_a_changed_file_fails(self) -> None:
        digest = self.add("docker.io/library/busybox:1.37")
        layer = self.layout.manifest_of(digest)["layers"][0]["digest"]
        self.layout.blobs[layer] = b"changed"

        self.assert_fails_once(self.verify(), str(layer).removeprefix("sha256:"))

    def test_a_missing_layer_fails(self) -> None:
        digest = self.add("docker.io/library/busybox:1.37")
        layer = self.layout.manifest_of(digest)["layers"][0]["digest"]
        del self.layout.blobs[layer]

        self.assert_fails_once(
            self.verify(), f"docker.io/library/busybox:1.37: its layer {layer} is missing"
        )

    def test_an_image_without_a_linux_amd64_manifest_fails(self) -> None:
        self.add("docker.io/library/busybox:1.37", architecture="arm64")

        self.assert_fails_once(
            self.verify(), "docker.io/library/busybox:1.37: no linux/amd64 manifest"
        )

    def test_a_single_manifest_for_another_platform_fails(self) -> None:
        self.add("ghcr.io/evoila/meho:v0.36.0", index=False, architecture="arm64")

        self.assert_fails_once(
            self.verify(),
            "ghcr.io/evoila/meho:v0.36.0: it is built for linux/arm64, not linux/amd64",
        )


class CheckScriptOptionTests(PackagingTestCase):
    def test_wrong_arguments_exit_2(self) -> None:
        named_wrong = self.shell.work / "download.tar"
        named_wrong.write_text("")
        cases = {
            "it needs one argument": [],
            "is not a file": [str(self.shell.work / "missing.tar")],
            "is not named like meho-linux-0.36.0-amd64.tar": [str(named_wrong)],
        }
        for fragment, args in cases.items():
            with self.subTest(args=args):
                self.assert_usage_error(self.shell.run_script("check-download.sh", *args), fragment)

    def test_a_download_above_the_limit_fails_before_anything_else(self) -> None:
        download = Path(self.shell.work / f"{TOP}.tar")
        download.write_bytes(b"x" * 100)

        result = self.shell.run_script(
            "check-download.sh", str(download), env={"MEHO_DOWNLOAD_MAX_BYTES": "99"}
        )

        self.assert_fails_once(result, "is 100 bytes", "The limit is 99 bytes")

    def test_help_lists_every_setting_the_script_reads(self) -> None:
        result = self.shell.run_script("check-download.sh", "--help")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("MEHO_DOWNLOAD_MAX_BYTES", result.stdout)
        self.assertIn("MEHO_DOWNLOAD_MAX_BYTES", (LINUX_SERVER / "check-download.sh").read_text())


if __name__ == "__main__":
    unittest.main()
