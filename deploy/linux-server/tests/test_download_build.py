#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of build-download.sh that run offline: the options, the size limit,
the image list and its digests, the licence table, and the build with an
overlay image and a registry login."""

from __future__ import annotations

import os
import re
import shutil
import unittest
from pathlib import Path

from download_harness import LINUX_SERVER, PACKAGING, REPO_ROOT, PackagingTestCase

BUSYBOX = "sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
PGVECTOR = "sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d"
VALKEY = "sha256:" + "1" * 64
MEHO = "sha256:" + "2" * 64
CHART = "sha256:" + "3" * 64
FULL_BUSYBOX = "docker.io/library/busybox"
CHART_DIR = REPO_ROOT / "deploy/charts/meho"

# A made-up overlay image and its signer.
OVERLAY = "registry.example.com/team/meho-overlay"
OVERLAY_SIGNER = (
    "https://git.example.com/team/connectors/.github/workflows/overlay.yml@refs/tags/v1.0.0"
)
OTHER_SIGNER = "https://git.example.com/someone/else/.github/workflows/build.yml@refs/heads/main"
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
PUBLIC_SIGNER = "https://github.com/evoila/meho/.github/workflows/image.yml@refs/tags/v0.36.0"
CHART_SIGNER = "https://github.com/evoila/meho/.github/workflows/chart.yml@refs/tags/v0.36.0"
OVERLAY_LICENCE = "Apache-2.0.txt,MEHO-NOTICE.txt https://example.com/meho-overlay/licence"
OVERLAY_ENV = {
    "MEHO_OVERLAY_IMAGE": OVERLAY,
    "MEHO_OVERLAY_SIGNER": OVERLAY_SIGNER,
    "MEHO_OVERLAY_LICENCE": OVERLAY_LICENCE,
}
PASSWORD = "made-up-registry-password-1234"


class OptionTests(PackagingTestCase):
    def test_help_lists_every_setting_the_script_reads(self) -> None:
        script = "".join(
            path.read_text()
            for path in (
                LINUX_SERVER / "build-download.sh",
                PACKAGING / "parts.sh",
                PACKAGING / "common.sh",
            )
        )
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


class OverlaySettingsTests(PackagingTestCase):
    SHOW = (
        'read_image_settings; printf "%s\\n" "$IMAGE_REPOSITORY" "$IMAGE_SIGNER" '
        '"$IMAGE_OIDC_ISSUER" "$IMAGE_LICENCE"'
    )

    def settings(self, env: dict[str, str]) -> list[str]:
        result = self.shell.run_snippet(self.SHOW, env={"SIGNER_REF": "refs/tags/v0.36.0", **env})
        self.assertEqual(0, result.returncode, self.output(result))
        return result.stdout.split("\n")[:4]

    def test_without_inputs_the_image_and_its_signer_are_the_public_ones(self) -> None:
        self.assertEqual(
            ["ghcr.io/evoila/meho", PUBLIC_SIGNER, GITHUB_ISSUER, ""], self.settings({})
        )

    def test_the_inputs_name_the_overlay_image_its_signer_and_its_licence(self) -> None:
        self.assertEqual(
            [OVERLAY, OVERLAY_SIGNER, GITHUB_ISSUER, OVERLAY_LICENCE], self.settings(OVERLAY_ENV)
        )
        issuer = "https://issuer.example.com"
        self.assertEqual(
            issuer, self.settings({**OVERLAY_ENV, "MEHO_OVERLAY_OIDC_ISSUER": issuer})[2]
        )

    def test_wrong_overlay_inputs_exit_2(self) -> None:
        cases = {
            "need MEHO_OVERLAY_IMAGE": {"MEHO_OVERLAY_SIGNER": OVERLAY_SIGNER},
            "MEHO_OVERLAY_SIGNER must name": {**OVERLAY_ENV, "MEHO_OVERLAY_SIGNER": ""},
            "MEHO_OVERLAY_LICENCE must be": {
                **OVERLAY_ENV,
                "MEHO_OVERLAY_LICENCE": "Apache-2.0.txt",
            },
            "names Unknown.txt, which is not in LICENSES/": {
                **OVERLAY_ENV,
                "MEHO_OVERLAY_LICENCE": "Unknown.txt https://example.com",
            },
            "without a tag": {**OVERLAY_ENV, "MEHO_OVERLAY_IMAGE": f"{OVERLAY}:v1"},
            "with its registry": {**OVERLAY_ENV, "MEHO_OVERLAY_IMAGE": "meho-overlay"},
        }
        for fragment, env in cases.items():
            with self.subTest(fragment=fragment):
                result = self.shell.run_snippet(
                    "read_image_settings", env={"SIGNER_REF": "refs/tags/v0.36.0", **env}
                )
                self.assert_usage_error(result, fragment)

    def test_the_script_checks_the_overlay_inputs_before_it_starts(self) -> None:
        cli = self.shell.work / "meho"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)

        result = self.shell.run_script(
            "build-download.sh",
            "0.36.0",
            "out",
            env={"MEHO_CLI_BINARY": str(cli), "MEHO_OVERLAY_IMAGE": OVERLAY},
        )

        self.assert_usage_error(result, "MEHO_OVERLAY_SIGNER must name")


class SignatureTests(PackagingTestCase):
    """wait_until_published checks the chart and the image with cosign."""

    def publish(self, image: str, signers: str, env: dict[str, str]):
        self.shell.answer(
            "regctl_digests", f"ghcr.io/evoila/meho-chart:0.36.0 {CHART}\n{image}:v0.36.0 {MEHO}\n"
        )
        chart_line = f"ghcr.io/evoila/meho-chart@{CHART} {CHART_SIGNER} {GITHUB_ISSUER}\n"
        self.shell.answer("cosign_signers", chart_line + signers)
        return self.shell.run_snippet(
            "read_image_settings; wait_until_published",
            env={
                "CHART_VERSION": "0.36.0",
                "IMAGE_TAG": "v0.36.0",
                "SIGNER_REF": "refs/tags/v0.36.0",
                "WAIT_SECONDS": "0",
                "WORK": str(self.shell.work),
                **env,
            },
        )

    def test_without_inputs_the_image_must_be_signed_by_image_yml_of_this_repository(self) -> None:
        result = self.publish(
            "ghcr.io/evoila/meho",
            f"ghcr.io/evoila/meho@{MEHO} {PUBLIC_SIGNER} {GITHUB_ISSUER}\n",
            {},
        )

        self.assert_succeeds(result)
        self.assertIn(f"--certificate-identity {PUBLIC_SIGNER}", self.shell.calls())

    def test_an_overlay_signed_by_the_given_identity_passes(self) -> None:
        result = self.publish(
            OVERLAY, f"{OVERLAY}@{MEHO} {OVERLAY_SIGNER} {GITHUB_ISSUER}\n", OVERLAY_ENV
        )

        self.assert_succeeds(result)
        self.assertIn(f"is signed by {OVERLAY_SIGNER}", result.stdout)
        self.assertIn(
            f"cosign verify {OVERLAY}@{MEHO} --certificate-identity {OVERLAY_SIGNER}",
            self.shell.calls(),
        )

    def test_an_overlay_of_another_signer_stops_the_build_and_names_the_expected_one(self) -> None:
        result = self.publish(
            OVERLAY, f"{OVERLAY}@{MEHO} {OTHER_SIGNER} {GITHUB_ISSUER}\n", OVERLAY_ENV
        )

        self.assert_fails_once(
            result, f"{OVERLAY}@{MEHO}", f"no valid signature from {OVERLAY_SIGNER}"
        )

    def test_an_overlay_signed_by_the_public_workflow_does_not_pass(self) -> None:
        result = self.publish(
            OVERLAY, f"{OVERLAY}@{MEHO} {PUBLIC_SIGNER} {GITHUB_ISSUER}\n", OVERLAY_ENV
        )

        self.assert_fails_once(result, OVERLAY_SIGNER)


class RegistryLoginTests(PackagingTestCase):
    LOGIN = {
        "MEHO_REGISTRY": "registry.example.com",
        "MEHO_REGISTRY_USERNAME": "robot",
        "MEHO_REGISTRY_PASSWORD": PASSWORD,
    }

    def test_the_password_goes_on_stdin_and_never_on_a_command_line_or_into_the_log(self) -> None:
        folder = self.shell.work / "login"

        result = self.shell.run_snippet(
            'registry_login "$1" regctl cosign\n'
            'printf "password=%s config=%s docker=%s\\n" "${MEHO_REGISTRY_PASSWORD-gone}" '
            '"$REGCTL_CONFIG" "$DOCKER_CONFIG"',
            str(folder),
            env=self.LOGIN,
        )

        self.assert_succeeds(result)
        calls = self.shell.calls()
        self.assertIn("regctl registry login registry.example.com --user robot --pass-stdin", calls)
        self.assertIn("cosign login registry.example.com --username robot --password-stdin", calls)
        self.assertNotIn(PASSWORD, calls)
        self.assertNotIn(PASSWORD, self.output(result))
        self.assertEqual(PASSWORD, (self.shell.fake / "regctl_login_stdin").read_text())
        self.assertEqual(PASSWORD, (self.shell.fake / "cosign_login_stdin").read_text())
        self.assertIn(
            f"password=gone config={folder}/regctl/config.json docker={folder}/docker",
            result.stdout,
        )

    def test_a_failed_login_stops_with_one_line_that_names_the_registry(self) -> None:
        self.shell.answer("regctl_login_rc", "1")

        result = self.shell.run(
            "registry_login", str(self.shell.work / "login"), "regctl", env=self.LOGIN
        )

        self.assert_fails_once(result, "regctl could not log in to registry.example.com as robot")
        self.assertNotIn(PASSWORD, self.output(result))

    def test_without_a_registry_there_is_no_login(self) -> None:
        result = self.shell.run("registry_login", str(self.shell.work / "login"), "regctl")

        self.assert_succeeds(result)
        self.assertEqual("", self.shell.calls())

    def test_a_login_needs_all_three_settings(self) -> None:
        for missing in self.LOGIN:
            with self.subTest(missing=missing):
                env = {**self.LOGIN, missing: ""}
                result = self.shell.run("check_registry_login", env=env)
                self.assert_usage_error(result, "must be set together")
                self.assertNotIn(PASSWORD, self.output(result))


class OverlayChartTests(PackagingTestCase):
    def values_copy(self) -> str:
        path = self.shell.work / "values-single-node.yaml"
        shutil.copy(LINUX_SERVER / "values-single-node.yaml", path)
        return str(path)

    def test_without_an_overlay_the_values_file_is_unchanged(self) -> None:
        values = self.values_copy()

        result = self.shell.run("name_overlay_image", values)

        self.assert_succeeds(result)
        self.assertEqual(
            (LINUX_SERVER / "values-single-node.yaml").read_text(), Path(values).read_text()
        )

    @unittest.skipUnless(
        shutil.which("helm") or os.environ.get("MEHO_REQUIRE_HELM") == "1",
        "helm is not on PATH; set MEHO_REQUIRE_HELM=1 to fail instead",
    )
    def test_with_an_overlay_the_chart_uses_the_overlay_image_and_not_the_public_one(self) -> None:
        values = self.values_copy()
        helm = shutil.which("helm") or "helm"

        result = self.shell.run_snippet(
            'name_overlay_image "$2" && chart_image_refs "$1" "$3" "$2" v0.36.0',
            helm,
            values,
            str(CHART_DIR),
            env={"IMAGE_REPOSITORY": OVERLAY},
        )

        self.assert_succeeds(result)
        images = set(result.stdout.split())
        self.assertIn(f"{OVERLAY}:v0.36.0", images)
        self.assertEqual(
            [], [image for image in images if image.startswith("ghcr.io/evoila/meho:")]
        )

    def test_with_an_overlay_the_licence_list_names_it_with_its_own_line(self) -> None:
        lock = self.shell.work / "images.lock"
        lock.write_text(f"docker.io/library/busybox:1.37 {BUSYBOX}\n{OVERLAY}:v0.36.0 {MEHO}\n")

        result = self.shell.run(
            "write_licence_readme",
            str(lock),
            env={
                "VERSION": "0.36.0",
                "CHART_VERSION": "0.36.0",
                "IMAGE_REPOSITORY": OVERLAY,
                "IMAGE_LICENCE": OVERLAY_LICENCE,
            },
        )

        self.assert_succeeds(result)
        self.assertIn(
            f"{OVERLAY}:v0.36.0 ({MEHO})\n"
            "  Licence: Apache-2.0.txt and MEHO-NOTICE.txt\n"
            "  Source:  https://example.com/meho-overlay/licence",
            result.stdout,
        )
        self.assertIn("Licence: GPL-2.0-BusyBox.txt", result.stdout)


if __name__ == "__main__":
    unittest.main()
