#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of --os-packages, the OS updates of the appliance in an update
(form-2 design, section 7, step 3; form-1 design, section 7).

The file is a tar of a flat apt repository: the .deb files, the package
list Packages (Packages.gz may be there too) and InRelease, the index that
evoila's key signs. install.sh points apt at the unpacked repository for
this one run, with a sources list of its own, and checks the signature
with the key in /etc/apt/keyrings (signed-by, never trusted=yes).

OsPackagesTests use a stub apt-get. RealAptTests run the real apt-get and
gpg against a repository that the test builds and signs. They need apt-get,
gpg and gpgv, and they are skipped without them. The CI job sets
MEHO_REQUIRE_APT_CHECK=1, which turns that skip into a failure.
"""

from __future__ import annotations

import email.utils
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

from update_harness import INSTALLER, UpdateTestCase, run_update_lib

KEYRING = "/etc/apt/keyrings/meho-os-packages.gpg"
REQUIRED = os.environ.get("MEHO_REQUIRE_APT_CHECK") == "1"
DEB = "linux-image-6.8.0-90-generic_6.8.0-90.91_amd64.deb"
PACKAGE_FIX = "use the OS-package file of the MEHO appliance release as it was published"

# The stub keeps what apt-get saw: the sources list, the files of the
# repository, and DEBIAN_FRONTEND. "update" writes a package list into the
# list folder, as apt does for an InRelease that names Packages, under the
# name in apt_package_list. "-s dist-upgrade" prints apt_simulation.
APT_GET_STUB = textwrap.dedent("""\
    list=""
    lists=""
    for arg; do
      case "$arg" in
        Dir::Etc::SourceList=*) list=${arg#*=} ;;
        Dir::State::Lists=*) lists=${arg#*=} ;;
      esac
    done
    case " $* " in
      *" update "*)
        cat "$list" > "$MEHO_FAKE/apt_sources_seen"
        for file in "${list%/*}"/repo/*; do echo "${file##*/}"; done > "$MEHO_FAKE/apt_repo_seen"
        rc=$(cat "$MEHO_FAKE/apt_update_rc")
        [ "$rc" = 0 ] || exit "$rc"
        name=$(cat "$MEHO_FAKE/apt_package_list")
        if [ -n "$name" ]; then
          : > "$lists/$name"
        fi
        exit 0 ;;
      *" dist-upgrade "*)
        cat "$MEHO_FAKE/apt_simulation"
        exit "$(cat "$MEHO_FAKE/apt_simulation_rc")" ;;
      *" upgrade "*)
        printf '%s\\n' "${DEBIAN_FRONTEND:-}" > "$MEHO_FAKE/apt_frontend_seen"
        exit "$(cat "$MEHO_FAKE/apt_upgrade_rc")" ;;
    esac
    exit 1
    """)


def tar_bytes(members: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    """A tar with the files and the symbolic links, in the order given."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


def flat_repository() -> dict[str, bytes]:
    return {
        "./InRelease": b"signed index\n",
        "./Packages": b"Package: linux-image\n",
        "./Packages.gz": gzip.compress(b"Package: linux-image\n"),
        f"./{DEB}": b"fake deb\n",
    }


class OsPackagesTests(UpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.machine.add_stub("apt-get", APT_GET_STUB)
        for name, value in {
            "apt_update_rc": "0",
            "apt_package_list": "_var_tmp_repo_._Packages",
            "apt_upgrade_rc": "0",
            "apt_simulation": "Reading package lists...\n0 upgraded, 0 newly installed\n",
            "apt_simulation_rc": "0",
        }.items():
            self.machine.answer(name, value)
        self.machine.host_file(KEYRING, "fake key\n")
        self.package_file = self.machine.home / "meho-appliance-0.37.0-os-packages.tar"
        self.package_file.write_bytes(tar_bytes(flat_repository()))

    def run_update(self) -> subprocess.CompletedProcess[str]:
        return self.machine.run("--confirm-backup", "--os-packages", str(self.package_file))

    def work_folders(self) -> list[Path]:
        return list(self.machine.host_path("/var/tmp").glob("meho-os-packages.*"))

    def apt_calls(self) -> list[str]:
        return [line for line in self.machine.calls().splitlines() if line.startswith("apt-get ")]

    def test_the_packages_come_only_from_the_file_and_are_checked_with_the_key(self) -> None:
        result = self.run_update()

        self.assert_succeeds(result)
        update, upgrade, check = self.apt_calls()
        sources = (self.machine.fake / "apt_sources_seen").read_text()
        work = sources.split("file:", 1)[1].split(" ", 1)[0].removesuffix("/repo")
        self.assertEqual(
            f"deb [signed-by={self.machine.root}{KEYRING}] file:{work}/repo ./\n", sources
        )
        self.assertNotIn("trusted", sources)
        options = (
            f"-o Dir::Etc::SourceList={work}/os-packages.list"
            f" -o Dir::Etc::SourceParts={work}/sources.list.d"
            f" -o Dir::State::Lists={work}/lists"
            " -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache="
        )
        self.assertEqual(f"apt-get {options} update --error-on=any", update)
        self.assertEqual(
            f"apt-get {options} -y -q -o Dpkg::Options::=--force-confdef"
            " -o Dpkg::Options::=--force-confold upgrade --with-new-pkgs",
            upgrade,
        )
        self.assertEqual(f"apt-get {options} -s -q dist-upgrade", check)
        self.assertIn("[ok]   installed the OS updates from the OS-package file", result.stdout)
        self.assertNotIn("[WARN]", result.stderr)
        self.assertEqual(
            ["InRelease", "Packages", "Packages.gz", DEB],
            (self.machine.fake / "apt_repo_seen").read_text().split(),
        )
        self.assertEqual("noninteractive\n", (self.machine.fake / "apt_frontend_seen").read_text())
        self.assertEqual([], self.work_folders())
        self.assertIn("== Update step 3: install the OS packages of the appliance", result.stdout)
        self.assertIn("is a flat apt repository. Number of .deb files: 1", result.stdout)

    def test_the_packages_come_after_the_backup_and_before_rke2(self) -> None:
        result = self.run_update()

        self.assert_succeeds(result)
        self.assertLess(
            result.stdout.index("--confirm-backup: a VM snapshot"),
            result.stdout.index("apt checked the signature"),
        )
        calls = self.machine.calls()
        self.assertLess(calls.index("apt-get"), calls.index(INSTALLER))

    def test_without_the_key_the_update_stops_in_step_1(self) -> None:
        self.machine.host_path(KEYRING).unlink()
        before = self.snapshot()

        line = self.assert_stopped_before_any_change(
            self.run_update(),
            before,
            f"--os-packages needs evoila's key for the OS packages in {KEYRING}",
        )

        self.assertIn("On a Linux server, leave out --os-packages", line)

    def test_a_file_that_is_not_a_flat_repository_stops_in_step_1(self) -> None:
        complete = flat_repository()
        without = {name: {k: v for k, v in complete.items() if k != name} for name in complete}
        cases: dict[str, tuple[bytes, str]] = {
            "not a tar": (b"not a tar file\n", "is not a tar file that install.sh can read."),
            "a folder": (
                tar_bytes({**complete, "./pool/main/a.deb": b"x"}),
                "holds ./pool/main/a.deb, which is not a plain file",
            ),
            "a way out": (
                tar_bytes({**complete, "../evil.deb": b"x"}),
                "holds ../evil.deb, which is not a plain file",
            ),
            "a link": (
                tar_bytes(complete, {"./passwd.deb": "/etc/passwd"}),
                "holds ./passwd.deb, which is not a plain file",
            ),
            "no InRelease": (
                tar_bytes(without["./InRelease"]),
                "is not a complete apt repository. Missing: InRelease.",
            ),
            "no Packages": (
                tar_bytes(without["./Packages"]),
                "is not a complete apt repository. Missing: Packages.",
            ),
            "no package": (
                tar_bytes(without[f"./{DEB}"]),
                "is not a complete apt repository. Missing: a .deb file.",
            ),
        }
        before = self.snapshot()
        for case, (content, problem) in cases.items():
            with self.subTest(case=case):
                self.package_file.write_bytes(content)

                line = self.assert_stopped_before_any_change(self.run_update(), before, problem)

                self.assertIn(PACKAGE_FIX, line)

    def test_an_index_that_apt_refuses_stops_before_rke2(self) -> None:
        self.machine.answer("apt_update_rc", "100")

        line = self.assert_fails_once(
            self.run_update(), "apt could not read the OS-package file", PACKAGE_FIX
        )

        self.assertIn("the signature or a checksum does not match", line)
        self.assertEqual(1, len(self.apt_calls()))
        self.assertNotIn(INSTALLER, self.machine.calls())
        self.assertEqual([], self.work_folders())

    def test_a_package_list_that_apt_keeps_compressed_is_found(self) -> None:
        self.machine.answer("apt_package_list", "_var_tmp_repo_._Packages.lz4")

        self.assert_succeeds(self.run_update())

    def test_an_index_without_an_uncompressed_package_list_stops(self) -> None:
        self.machine.answer("apt_package_list", "")

        self.assert_fails_once(
            self.run_update(), "InRelease names no uncompressed Packages file.", PACKAGE_FIX
        )
        self.assertEqual(1, len(self.apt_calls()))
        self.assertEqual([], self.work_folders())

    def test_a_failed_package_install_stops_before_rke2(self) -> None:
        self.machine.answer("apt_upgrade_rc", "100")

        line = self.assert_fails_once(self.run_update(), "apt could not install the OS packages.")

        self.assertIn("Fix: revert the VM snapshot", line)
        self.assertNotIn(INSTALLER, self.machine.calls())
        self.assertEqual([], self.work_folders())

    def test_packages_that_stay_at_their_old_version_are_named(self) -> None:
        self.machine.answer(
            "apt_simulation",
            "Inst openssh-server [1:9.6p1-3ubuntu13.5] (1:9.6p1-3ubuntu13.14 localhost [amd64])\n"
            "Inst libssl3t64 [3.0.13-0ubuntu3.4] (3.0.13-0ubuntu3.6 localhost [amd64])\n"
            "Remv libold1 [1.0]\n",
        )

        result = self.run_update()

        self.assert_succeeds(result)
        self.assertIn(
            "[WARN] These packages of the OS-package file stay at their old version, because apt"
            " would have to remove another package for them: openssh-server, libssl3t64.",
            result.stderr,
        )
        self.assertIn("[ok]   installed the other OS updates", result.stdout)

    def test_a_failed_check_of_the_packages_warns(self) -> None:
        self.machine.answer("apt_simulation_rc", "100")

        result = self.run_update()

        self.assert_succeeds(result)
        self.assertIn(
            "[WARN] apt could not check whether every package of the OS-package file is installed.",
            result.stderr,
        )

    def test_a_package_that_needs_a_restart_gets_a_note(self) -> None:
        self.machine.host_file("/run/reboot-required", "*** System restart required ***\n")

        result = self.run_update()

        self.assert_succeeds(result)
        self.assertIn("[note] the new OS packages need a restart of this machine.", result.stdout)

    def test_without_the_option_apt_does_not_run(self) -> None:
        result = self.machine.run("--confirm-backup")

        self.assert_succeeds(result)
        self.assertEqual([], self.apt_calls())
        self.assertNotIn("OS packages", result.stdout)


class RealAptTests(UpdateTestCase):
    """The real apt-get reads a flat repository that the test signs."""

    gnupg: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        missing = [tool for tool in ("apt-get", "gpg", "gpgv") if shutil.which(tool) is None]
        if missing:
            reason = f"{', '.join(missing)} not on PATH"
            if REQUIRED:
                raise AssertionError(f"MEHO_REQUIRE_APT_CHECK=1, but {reason}")
            raise unittest.SkipTest(reason)
        cls.gnupg = tempfile.TemporaryDirectory(prefix="meho-gpg-")
        for name in ("evoila", "other"):
            home = Path(cls.gnupg.name) / name
            home.mkdir(mode=0o700)
            cls.gpg(name, "--quick-generate-key", f"{name} <{name}@example.invalid>", "ed25519")

    @classmethod
    def tearDownClass(cls) -> None:
        for name in ("evoila", "other"):
            subprocess.run(
                ["gpgconf", "--kill", "gpg-agent"],
                env={**os.environ, "GNUPGHOME": str(Path(cls.gnupg.name) / name)},
                capture_output=True,
                check=False,
            )
        cls.gnupg.cleanup()

    @classmethod
    def gpg(cls, name: str, *args: str, stdin: bytes = b"") -> bytes:
        result = subprocess.run(
            ["gpg", "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase", "", *args],
            input=stdin,
            env={**os.environ, "GNUPGHOME": str(Path(cls.gnupg.name) / name)},
            capture_output=True,
            check=True,
            timeout=60,
        )
        return result.stdout

    def setUp(self) -> None:
        super().setUp()
        keyring = self.machine.host_path(KEYRING)
        keyring.parent.mkdir(parents=True)
        keyring.write_bytes(self.gpg("evoila", "--export"))
        self.package_file = self.machine.home / "os-packages.tar"

    @staticmethod
    def release(files: dict[str, bytes]) -> bytes:
        lines = [f"Date: {email.utils.formatdate(usegmt=True)}", "SHA256:"]
        for name, content in files.items():
            lines.append(f" {hashlib.sha256(content).hexdigest()} {len(content)} {name}")
        return ("\n".join(lines) + "\n").encode()

    def write_repository(self, signer: str, lists: tuple[str, ...], changed: bool = False) -> None:
        deb = b"fake deb\n"
        packages = textwrap.dedent(f"""\
            Package: meho-test
            Version: 1.0
            Architecture: all
            Maintainer: MEHO tests <tests@example.invalid>
            Filename: ./{DEB}
            Size: {len(deb)}
            SHA256: {hashlib.sha256(deb).hexdigest()}
            Description: a package for the test
            """).encode()
        files = {"Packages": packages, "Packages.gz": gzip.compress(packages)}
        files = {name: files[name] for name in lists}
        signed = self.gpg(signer, "--clearsign", stdin=self.release(files))
        if changed:
            files["Packages"] = packages.replace(b"1.0", b"9.9")
        members = {f"./{name}": content for name, content in files.items()}
        members.update({"./InRelease": signed, f"./{DEB}": deb})
        self.package_file.write_bytes(tar_bytes(members))

    def read_index(self, apt_config: str = "") -> subprocess.CompletedProcess[str]:
        """Unpack the file and read its index with the real apt. apt_config
        is more apt settings, for this run only (APT_CONFIG)."""
        config = self.machine.home / "apt.conf"
        config.write_text(apt_config)
        result = run_update_lib(
            self.machine,
            f'unpack_os_packages "{self.package_file}"\n'
            'read_os_packages_index "$OS_PACKAGES_WORK"\n',
            path=os.environ["PATH"],
            extra_env={"APT_CONFIG": str(config)},
        )
        self.assertEqual([], list(self.machine.host_path("/var/tmp").glob("meho-os-packages.*")))
        return result

    def test_a_repository_signed_with_the_key_is_read(self) -> None:
        self.write_repository("evoila", ("Packages", "Packages.gz"))

        result = self.read_index()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("[ok]   apt checked the signature of the OS-package file", result.stdout)

    def test_a_package_list_kept_compressed_is_found(self) -> None:
        self.write_repository("evoila", ("Packages", "Packages.gz"))

        result = self.read_index('Acquire::GzipIndexes "true";\n')

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("[ok]   apt checked the signature of the OS-package file", result.stdout)

    def test_a_repository_signed_with_another_key_is_refused(self) -> None:
        self.write_repository("other", ("Packages", "Packages.gz"))

        result = self.read_index()

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("[FAIL] apt could not read the OS-package file", result.stderr)
        self.assertIn("NO_PUBKEY", result.stderr)

    def test_a_package_list_changed_after_signing_is_refused(self) -> None:
        self.write_repository("evoila", ("Packages", "Packages.gz"), changed=True)

        result = self.read_index()

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("[FAIL] apt could not read the OS-package file", result.stderr)

    def test_an_index_with_only_a_compressed_package_list_is_refused(self) -> None:
        self.write_repository("evoila", ("Packages.gz",))

        result = self.read_index()

        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("InRelease names no uncompressed Packages file.", result.stderr)


if __name__ == "__main__":
    unittest.main()
