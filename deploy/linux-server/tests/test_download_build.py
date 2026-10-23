#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of build-download.sh that run offline: the options, the size limit,
the image list and its digests, and the licence table."""

from __future__ import annotations

import re
import unittest

from download_harness import LINUX_SERVER, PACKAGING, REPO_ROOT, PackagingTestCase

BUSYBOX = "sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
PGVECTOR = "sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d"
VALKEY = "sha256:" + "1" * 64
MEHO = "sha256:" + "2" * 64
FULL_BUSYBOX = "docker.io/library/busybox"


class OptionTests(PackagingTestCase):
    def test_help_lists_every_setting_the_script_reads(self) -> None:
        script = (LINUX_SERVER / "build-download.sh").read_text()
        settings = set(re.findall(r"\$\{(MEHO_[A-Z_]+)", script))

        result = self.shell.run_script("build-download.sh", "--help")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("MEHO_CLI_BINARY", settings, "the settings were not found in the script")
        self.assertEqual(
            settings, set(re.findall(r"^  (MEHO_[A-Z_]+)", result.stdout, re.MULTILINE))
        )

    def test_wrong_arguments_exit_2(self) -> None:
        cases = {
            "it needs two arguments": [],
            "not like 0.36.0 (no leading v)": ["v0.36.0", "out"],
            "MEHO_CLI_BINARY must name": ["0.36.0", "out"],
        }
        for fragment, args in cases.items():
            with self.subTest(args=args):
                self.assert_usage_error(self.shell.run_script("build-download.sh", *args), fragment)

    def test_a_wait_that_is_not_a_number_exits_2(self) -> None:
        result = self.shell.run_script(
            "build-download.sh", "0.36.0", "out", env={"MEHO_WAIT_SECONDS": "an hour"}
        )

        self.assert_usage_error(result, "MEHO_WAIT_SECONDS must be a number")


class SizeLimitTests(PackagingTestCase):
    def file_of(self, size: int) -> str:
        path = self.shell.work / "meho-linux-0.36.0-amd64.tar"
        path.write_bytes(b"x" * size)
        return str(path)

    def test_a_download_above_the_limit_fails_with_one_line_that_names_both_sizes(self) -> None:
        result = self.shell.run("check_size", self.file_of(1000), "999")

        line = self.assert_fails_once(result, "is 1000 bytes", "The limit is 999 bytes")
        self.assertIn("meho-linux-0.36.0-amd64.tar", line)

    def test_a_download_at_the_limit_passes(self) -> None:
        result = self.shell.run("check_size", self.file_of(1000), "1000")

        self.assert_succeeds(result)
        self.assertIn("[ok]", result.stdout)

    def test_the_default_limit_is_1_9_gib(self) -> None:
        result = self.shell.run_snippet('printf %s "$DEFAULT_MAX_BYTES"')

        self.assertEqual(str(19 * 2**30 // 10), result.stdout)


class ImageReferenceTests(PackagingTestCase):
    def test_references_get_their_full_form(self) -> None:
        cases = {
            "busybox": "docker.io/library/busybox latest -",
            "busybox:1.37": "docker.io/library/busybox 1.37 -",
            "valkey/valkey:9.0-alpine": "docker.io/valkey/valkey 9.0-alpine -",
            f"pgvector/pgvector@{PGVECTOR}": f"docker.io/pgvector/pgvector - {PGVECTOR}",
            f"docker.io/library/busybox:1.37@{BUSYBOX}": f"{FULL_BUSYBOX} 1.37 {BUSYBOX}",
            f"index.docker.io/library/busybox:1.37@{BUSYBOX}": f"{FULL_BUSYBOX} 1.37 {BUSYBOX}",
            "ghcr.io/evoila/meho:v0.36.0": "ghcr.io/evoila/meho v0.36.0 -",
            "quay.io/openbao/openbao-ubi:2.7.1": "quay.io/openbao/openbao-ubi 2.7.1 -",
            "localhost:5000/meho:1": "localhost:5000/meho 1 -",
        }
        for reference, expected in cases.items():
            with self.subTest(reference=reference):
                result = self.shell.run("normalize_image_ref", reference)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, result.stdout.strip())

    def test_malformed_references_are_refused(self) -> None:
        for reference in (
            "Busybox:1",
            "busybox@sha256:abc",
            "busybox:",
            "a//b",
            "busybox:1@md5:00",
        ):
            with self.subTest(reference=reference):
                result = self.shell.run("normalize_image_ref", reference)
                self.assertEqual(1, result.returncode)
                self.assertEqual("", result.stdout)

    def test_an_image_pinned_by_digest_only_gets_a_digest_tag(self) -> None:
        by_tag = self.shell.run("archive_name", "docker.io/library/busybox", "1.37", BUSYBOX)
        by_digest = self.shell.run("archive_name", "docker.io/pgvector/pgvector", "-", PGVECTOR)

        self.assertEqual("docker.io/library/busybox:1.37", by_tag.stdout.strip())
        self.assertEqual(
            "docker.io/pgvector/pgvector:digest-" + PGVECTOR.removeprefix("sha256:"),
            by_digest.stdout.strip(),
        )

    def test_every_container_image_of_a_manifest_is_listed(self) -> None:
        manifest = (
            "spec:\n"
            "  initContainers:\n"
            "    - name: init\n"
            '      image: "ghcr.io/evoila/meho:v0.36.0"\n'
            "  containers:\n"
            f"    - image: pgvector/pgvector@{PGVECTOR}\n"
            "      name: db\n"
            "    - name: cache\n"
            "      image: 'valkey/valkey:9.0-alpine'\n"
            "data:\n"
            "  note: the image: field is not read here\n"
        )
        path = self.shell.work / "rendered.yaml"
        path.write_text(manifest)

        result = self.shell.run_snippet('images_in_manifests < "$1"', str(path))

        self.assertEqual(
            [
                "ghcr.io/evoila/meho:v0.36.0",
                f"pgvector/pgvector@{PGVECTOR}",
                "valkey/valkey:9.0-alpine",
            ],
            result.stdout.split(),
        )


class LockTests(PackagingTestCase):
    def lock(self, *references: str) -> tuple[int, str, str]:
        refs = self.shell.work / "refs.txt"
        refs.write_text("".join(f"{reference}\n" for reference in references))
        lock = self.shell.work / "images.lock"
        result = self.shell.run("lock_images", str(refs), str(lock))
        content = lock.read_text() if lock.exists() else ""
        return result.returncode, content, self.output(result)

    def test_every_image_is_locked_to_a_digest(self) -> None:
        self.shell.answer(
            "regctl_digests",
            f"docker.io/valkey/valkey:9.0-alpine {VALKEY}\nghcr.io/evoila/meho:v0.36.0 {MEHO}\n",
        )

        code, content, output = self.lock(
            "valkey/valkey:9.0-alpine",
            f"pgvector/pgvector@{PGVECTOR}",
            f"docker.io/library/busybox:1.37@{BUSYBOX}",
            "ghcr.io/evoila/meho:v0.36.0",
            "valkey/valkey:9.0-alpine",
        )

        self.assertEqual(0, code, output)
        self.assertEqual(
            [
                f"docker.io/library/busybox:1.37 {BUSYBOX}",
                f"docker.io/pgvector/pgvector:digest-{PGVECTOR.removeprefix('sha256:')} {PGVECTOR}",
                f"docker.io/valkey/valkey:9.0-alpine {VALKEY}",
                f"ghcr.io/evoila/meho:v0.36.0 {MEHO}",
            ],
            content.splitlines(),
        )

    def test_only_images_without_a_digest_are_asked_at_the_registry(self) -> None:
        self.shell.answer("regctl_digests", f"docker.io/valkey/valkey:9.0-alpine {VALKEY}\n")

        self.lock("valkey/valkey:9.0-alpine", f"docker.io/library/busybox:1.37@{BUSYBOX}")

        self.assertEqual(
            "regctl image digest docker.io/valkey/valkey:9.0-alpine\n", self.shell.calls()
        )

    def test_a_registry_that_does_not_answer_stops_the_build(self) -> None:
        refs = self.shell.work / "refs.txt"
        refs.write_text("valkey/valkey:9.0-alpine\n")

        result = self.shell.run("lock_images", str(refs), str(self.shell.work / "images.lock"))

        self.assert_fails_once(result, "docker.io/valkey/valkey:9.0-alpine")

    def test_a_malformed_reference_stops_the_build(self) -> None:
        refs = self.shell.work / "refs.txt"
        refs.write_text("Busybox:1\n")

        result = self.shell.run("lock_images", str(refs), str(self.shell.work / "images.lock"))

        self.assert_fails_once(result, "'Busybox:1' is not valid")

    def test_one_name_with_two_digests_stops_the_build(self) -> None:
        refs = self.shell.work / "refs.txt"
        refs.write_text(f"busybox:1.37@{BUSYBOX}\nbusybox:1.37@{PGVECTOR}\n")

        result = self.shell.run("lock_images", str(refs), str(self.shell.work / "images.lock"))

        self.assert_fails_once(result, "docker.io/library/busybox:1.37")


class LicenceTests(PackagingTestCase):
    @staticmethod
    def licence_lines() -> dict[str, list[str]]:
        lines = {}
        for line in (PACKAGING / "image-sources.txt").read_text().splitlines():
            if line and not line.startswith("#"):
                repository, licences, *_ = line.split()
                lines[repository] = licences.split(",")
        return lines

    def test_every_licence_file_named_in_the_table_ships(self) -> None:
        shipped = {path.name for path in (PACKAGING / "licenses").glob("*.txt")}
        shipped |= {"Apache-2.0.txt", "MEHO-NOTICE.txt"}
        self.assertTrue((REPO_ROOT / "LICENSE").is_file() and (REPO_ROOT / "NOTICE").is_file())

        for repository, licences in self.licence_lines().items():
            with self.subTest(repository=repository):
                self.assertLessEqual(set(licences), shipped)

    def test_every_image_repository_of_the_chart_and_the_storage_manifest_has_a_line(self) -> None:
        sources = [*(REPO_ROOT / "deploy/charts/meho").rglob("values.yaml")]
        found = set()
        for source in sources:
            found |= set(re.findall(r"^\s*repository:\s*(\S+)", source.read_text(), re.MULTILINE))
        storage = (LINUX_SERVER / "storage/local-path-storage.yaml").read_text()
        found |= {image.split(":")[0] for image in re.findall(r"image:\s*(\S+)", storage)}
        self.assertIn("ghcr.io/evoila/meho", found)

        for repository in sorted(found):
            with self.subTest(repository=repository):
                result = self.shell.run("normalize_image_ref", repository)
                full = result.stdout.split()[0]
                self.assertIn(full, self.licence_lines())

    def test_the_readme_names_each_image_with_its_digest_licence_and_source(self) -> None:
        lock = self.shell.work / "images.lock"
        lock.write_text(
            f"docker.io/library/busybox:1.37 {BUSYBOX}\nghcr.io/evoila/meho:v0.36.0 {MEHO}\n"
        )

        result = self.shell.run(
            "write_licence_readme", str(lock), env={"VERSION": "0.36.0", "CHART_VERSION": "0.36.0"}
        )

        self.assert_succeeds(result)
        self.assertIn(
            f"docker.io/library/busybox:1.37 ({BUSYBOX})\n  Licence: GPL-2.0-BusyBox.txt",
            result.stdout,
        )
        self.assertIn("Licence: Apache-2.0.txt and MEHO-NOTICE.txt", result.stdout)
        self.assertIn("Source:  https://github.com/rancher/rke2/tree/v1.37.1+rke2r1", result.stdout)
        self.assertIn("Licences and source code of meho-linux-0.36.0-amd64", result.stdout)

    def test_an_image_without_a_licence_line_stops_the_build(self) -> None:
        lock = self.shell.work / "images.lock"
        lock.write_text(f"docker.io/library/unknown:1 {BUSYBOX}\n")

        result = self.shell.run(
            "write_licence_readme", str(lock), env={"VERSION": "0.36.0", "CHART_VERSION": "0.36.0"}
        )

        self.assert_fails_once(
            result, "No licence is known for the image docker.io/library/unknown"
        )


if __name__ == "__main__":
    unittest.main()
