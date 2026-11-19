#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Tests of os-packages.sh and upload-release.sh.

os-packages.sh runs for real with gpg, apt-ftparchive and apt-get. The test
keys are made fresh in each run and never leave the temporary folder. The
test key's user id says it is a test key; the release path refuses it.
upload-release.sh runs against a stand-in for gcloud that keeps its
"bucket" in a local folder. They need gpg, apt-ftparchive, dpkg-deb and
apt-get; the CI job sets MEHO_REQUIRE_APT_CHECK=1, which turns a skip into
a failure.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

APPLIANCE = Path(__file__).resolve().parents[1]
OS_PACKAGES = APPLIANCE / "os-packages.sh"
UPLOAD = APPLIANCE / "upload-release.sh"
WORKFLOW = APPLIANCE.parents[1] / ".github" / "workflows" / "appliance-build.yml"
LINUX_SERVER = APPLIANCE.parent / "linux-server"
VERSION = "0.38.0"
TEST_UID = "MEHO TEST KEY - NEVER FOR A RELEASE <test-key@example.invalid>"
OTHER_UID = "MEHO other signer <signer@example.invalid>"
REQUIRED = os.environ.get("MEHO_REQUIRE_APT_CHECK") == "1"
TOOLS = ("gpg", "apt-ftparchive", "dpkg-deb", "apt-get")

STUB_GCLOUD = """#!/usr/bin/env bash
# A stand-in for "gcloud storage": the bucket is the folder $STORE.
echo "$*" >>"$GCLOUD_LOG"
[[ $1 == storage ]] || exit 2
shift
case $1 in
    ls)
        [[ -z ${GCLOUD_FAIL:-} ]] || {
            echo "ERROR: (gcloud.storage.ls) [sa@example.invalid] does not have permission" >&2
            exit 1
        }
        path=${2#gs://}
        [[ -d $STORE/${path%%/*} ]] || {
            echo "ERROR: (gcloud.storage.ls) gs://${path%%/*} not found: 404." >&2
            exit 1
        }
        [[ -e $STORE/$path ]] || {
            echo "ERROR: (gcloud.storage.ls) One or more URLs matched no objects." >&2
            exit 1
        }
        echo "$2" ;;
    cp)
        shift
        clobber=1
        [[ $1 == --no-clobber ]] && { clobber=0; shift; }
        src=$1 dst=$2
        if [[ $src == gs://* ]]; then
            cp "$STORE/${src#gs://}" "$dst"
        else
            target=$STORE/${dst#gs://}
            mkdir -p "$(dirname "$target")"
            if [[ ( -e $target || -n ${GCLOUD_SKIP:-} ) && $clobber == 0 ]]; then
                echo "Skipping existing destination item (no-clobber): $dst" >&2
                exit 0
            fi
            cp "$src" "$target"
        fi ;;
    *) exit 2 ;;
esac
"""


def missing_tools() -> list[str]:
    return [tool for tool in TOOLS if shutil.which(tool) is None]


def setUpModule() -> None:
    missing = missing_tools()
    if missing:
        reason = f"{', '.join(missing)} not on PATH"
        if REQUIRED:
            raise AssertionError(f"MEHO_REQUIRE_APT_CHECK=1, but {reason}")
        raise unittest.SkipTest(reason)


class Gpg:
    """Keys in their own home folders, made fresh for the test."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def home(self, name: str) -> Path:
        return self.root / name

    def run(self, name: str, *args: str) -> bytes:
        home = self.home(name)
        home.mkdir(mode=0o700, exist_ok=True)
        result = subprocess.run(
            [
                "gpg",
                "--batch",
                "--quiet",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                *args,
            ],
            env={**os.environ, "GNUPGHOME": str(home)},
            capture_output=True,
            check=True,
            timeout=60,
        )
        return result.stdout

    def fingerprint(self, name: str) -> str:
        lines = self.run(name, "--list-secret-keys", "--with-colons").decode()
        return next(
            line.split(":")[9] for line in lines.splitlines() if line.startswith("fpr:")
        )

    def make_key(self, name: str, uid: str) -> tuple[str, bytes]:
        self.run(name, "--quick-generate-key", uid, "ed25519")
        return self.run(name, "--armor", "--export-secret-keys").decode(), self.run(
            name, "--export"
        )

    def stop(self) -> None:
        for home in self.root.iterdir():
            subprocess.run(
                ["gpgconf", "--kill", "gpg-agent"],
                env={**os.environ, "GNUPGHOME": str(home)},
                capture_output=True,
                check=False,
            )


class ReleaseFilesCase(unittest.TestCase):
    gpg: Gpg
    keys: dict[str, tuple[str, bytes]]
    keys_dir: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.keys_dir = tempfile.TemporaryDirectory(prefix="meho-gpg-")
        cls.gpg = Gpg(Path(cls.keys_dir.name))
        cls.keys = {
            "test": cls.gpg.make_key("test", TEST_UID),
            "real": cls.gpg.make_key("real", OTHER_UID),
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.gpg.stop()
        cls.keys_dir.cleanup()

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.debs = self.root / "debs"
        self.out = self.root / "out"
        self.debs.mkdir()


class OsPackagesTests(ReleaseFilesCase):
    def make_deb(
        self, name: str = "meho-test", version: str = "1.0", file_name: str = ""
    ) -> Path:
        tree = self.root / f"{name}-tree"
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "DEBIAN" / "control").write_text(
            f"Package: {name}\nVersion: {version}\nArchitecture: all\n"
            "Maintainer: MEHO tests <tests@example.invalid>\nDescription: a package for the test\n"
        )
        (tree / "payload.txt").write_text("payload\n")
        deb = self.debs / (file_name or f"{name}_{version}_all.deb")
        subprocess.run(
            ["dpkg-deb", "--build", str(tree), str(deb)],
            check=True,
            capture_output=True,
        )
        return deb

    def run_script(
        self, key: str | None, release: bool = False, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        """A release run gets the fingerprint of the "real" key, unless the
        test gives MEHO_OS_PACKAGES_KEY_FINGERPRINT itself."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEHO_")}
        if key is not None:
            env["MEHO_OS_PACKAGES_SIGNING_KEY"] = key
        if release:
            env["MEHO_RELEASE"] = "1"
            env["MEHO_OS_PACKAGES_KEY_FINGERPRINT"] = self.gpg.fingerprint("real")
        env.update(extra)
        return subprocess.run(
            [str(OS_PACKAGES), VERSION, str(self.debs), str(self.out)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    @property
    def package_file(self) -> Path:
        return self.out / f"meho-appliance-{VERSION}-os-packages.tar"

    def unpack(self, folder: Path) -> Path:
        with tarfile.open(self.package_file) as archive:
            archive.extractall(folder, filter="data")
        return folder

    def apt_update(
        self, repo: Path, public_key: bytes
    ) -> subprocess.CompletedProcess[str]:
        """Read the repository with the real apt, as install.sh does it: a
        sources list of its own and signed-by with the public key."""
        keyring = self.root / "keyring.gpg"
        keyring.write_bytes(public_key)
        lists = self.root / "lists"
        (lists / "partial").mkdir(parents=True, exist_ok=True)
        sources = self.root / "os-packages.list"
        sources.write_text(f"deb [signed-by={keyring}] file:{repo} ./\n")
        self.apt_options = [
            "-o", f"Dir::Etc::SourceList={sources}",
            "-o", f"Dir::Etc::SourceParts={self.root / 'sources.list.d'}",
            "-o", f"Dir::State::Lists={lists}",
            "-o", f"Dir::Cache={self.root / 'cache'}",
            "-o", f"Dir::State::status={self.root / 'status'}",
            "-o", "APT::Sandbox::User=",
        ]  # fmt: skip
        (self.root / "status").write_text("")
        return subprocess.run(
            ["apt-get", *self.apt_options, "update", "--error-on=any"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_apt_accepts_the_file_with_the_matching_public_key(self) -> None:
        self.make_deb()
        key, public = self.keys["test"]

        result = self.run_script(key)

        self.assertEqual(0, result.returncode, result.stderr)
        with tarfile.open(self.package_file) as archive:
            names = archive.getnames()
        self.assertEqual(
            ["./InRelease", "./Packages", "./meho-test_1.0_all.deb"], names
        )
        repo = self.unpack(self.root / "repo")
        self.assertIn(" Packages\n", (repo / "InRelease").read_text())
        update = self.apt_update(repo, public)
        self.assertEqual(0, update.returncode, update.stderr)
        self.assertIn("meho-test", subprocess.run(
            ["apt-cache", *self.apt_options, "policy", "meho-test"],
            capture_output=True, text=True, check=False,
        ).stdout)  # fmt: skip

    def installer_check(self) -> subprocess.CompletedProcess[str]:
        """Run the archive check of install.sh --os-packages on the file."""
        machine = self.root / "machine"
        keyring = machine / "etc/apt/keyrings/meho-os-packages.gpg"
        keyring.parent.mkdir(parents=True)
        keyring.write_bytes(b"key\n")
        return subprocess.run(
            [
                "bash",
                "-c",
                'source lib/common.sh && source lib/update.sh && check_os_packages "$1"',
                "check",
                str(self.package_file),
            ],
            cwd=LINUX_SERVER,
            env={**os.environ, "MEHO_TEST_ROOT": str(machine)},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_a_package_with_an_epoch_gets_a_name_the_installer_accepts(
        self,
    ) -> None:
        # apt download names this package meho-test_2%3a1.0_all.deb.
        self.make_deb(version="2:1.0", file_name="meho-test_2%3a1.0_all.deb")
        key, public = self.keys["test"]

        result = self.run_script(key)

        self.assertEqual(0, result.returncode, result.stderr)
        with tarfile.open(self.package_file) as archive:
            names = archive.getnames()
        self.assertEqual(
            ["./InRelease", "./Packages", "./meho-test_1.0_all.deb"], names
        )
        check = self.installer_check()
        self.assertEqual(0, check.returncode, check.stderr)
        self.assertIn("Number of .deb files: 1", check.stdout)
        repo = self.unpack(self.root / "repo")
        self.assertIn(
            "Filename: ./meho-test_1.0_all.deb", (repo / "Packages").read_text()
        )
        self.assertEqual(0, self.apt_update(repo, public).returncode)

    def test_two_files_of_the_same_package_version_stop(self) -> None:
        deb = self.make_deb()
        shutil.copy(deb, self.debs / "copy.deb")

        result = self.run_script(self.keys["test"][0])

        self.assertEqual(1, result.returncode)
        self.assertIn("the same package version: meho-test_1.0_all.deb", result.stderr)
        self.assertFalse(self.package_file.exists())

    def test_a_file_that_is_not_a_package_stops(self) -> None:
        (self.debs / "broken.deb").write_bytes(b"not a package\n")

        result = self.run_script(self.keys["test"][0])

        self.assertEqual(1, result.returncode)
        self.assertIn("dpkg-deb cannot read broken.deb", result.stderr)
        self.assertFalse(self.package_file.exists())

    def test_apt_refuses_the_file_with_another_public_key(self) -> None:
        self.make_deb()
        self.run_script(self.keys["test"][0])
        repo = self.unpack(self.root / "repo")

        update = self.apt_update(repo, self.keys["real"][1])

        self.assertNotEqual(0, update.returncode)

    def test_apt_refuses_a_deb_changed_after_signing(self) -> None:
        deb = self.make_deb()
        key, public = self.keys["test"]
        self.run_script(key)
        repo = self.unpack(self.root / "repo")
        (repo / deb.name).write_bytes(deb.read_bytes() + b"changed")
        self.assertEqual(0, self.apt_update(repo, public).returncode)

        download = self.root / "download"
        download.mkdir()
        result = subprocess.run(
            ["apt-get", *self.apt_options, "download", "meho-test"],
            cwd=download, capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip

        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], list(download.iterdir()))

    def test_the_key_can_come_from_a_file(self) -> None:
        self.make_deb()
        key_file = self.root / "key.asc"
        key_file.write_text(self.keys["test"][0])

        result = self.run_script(None, MEHO_OS_PACKAGES_SIGNING_KEY_FILE=str(key_file))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.package_file.is_file())

    def test_a_release_with_the_test_key_stops(self) -> None:
        self.make_deb()

        # Even when the fingerprint variable names the test key.
        result = self.run_script(
            self.keys["test"][0],
            release=True,
            MEHO_OS_PACKAGES_KEY_FINGERPRINT=self.gpg.fingerprint("test"),
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("This is a test key", result.stderr)
        self.assertFalse(self.package_file.exists())

    def test_a_release_with_another_key_is_signed(self) -> None:
        self.make_deb()
        key, public = self.keys["real"]

        result = self.run_script(key, release=True)

        self.assertEqual(0, result.returncode, result.stderr)
        repo = self.unpack(self.root / "repo")
        self.assertEqual(0, self.apt_update(repo, public).returncode)

    def test_a_release_with_the_fingerprint_in_another_form_is_signed(
        self,
    ) -> None:
        self.make_deb()
        fingerprint = self.gpg.fingerprint("real").lower()
        spaced = " ".join(fingerprint[i : i + 4] for i in range(0, 40, 4))

        result = self.run_script(
            self.keys["real"][0], release=True, MEHO_OS_PACKAGES_KEY_FINGERPRINT=spaced
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_a_release_with_a_key_of_another_fingerprint_stops(self) -> None:
        self.make_deb()
        (self.root / "gpg").mkdir()
        other = Gpg(self.root / "gpg")
        self.addCleanup(other.stop)
        key, _ = other.make_key("other", OTHER_UID)

        result = self.run_script(key, release=True)

        self.assertEqual(1, result.returncode)
        self.assertIn("The signing key is not the real key", result.stderr)
        self.assertFalse(self.package_file.exists())

    def test_a_release_without_the_fingerprint_stops(self) -> None:
        self.make_deb()

        for value in ("", "not a fingerprint"):
            with self.subTest(value=value):
                result = self.run_script(
                    self.keys["real"][0],
                    release=True,
                    MEHO_OS_PACKAGES_KEY_FINGERPRINT=value,
                )

                self.assertEqual(1, result.returncode)
                self.assertIn(
                    "The fingerprint of the real signing key is not set up",
                    result.stderr,
                )
        self.assertFalse(self.package_file.exists())

    def test_a_release_never_reads_the_key_file(self) -> None:
        self.make_deb()
        key_file = self.root / "key.asc"
        key_file.write_text(self.keys["real"][0])

        result = self.run_script(
            None, release=True, MEHO_OS_PACKAGES_SIGNING_KEY_FILE=str(key_file)
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("The signing key is not set up", result.stderr)
        self.assertFalse(self.package_file.exists())

    def test_a_release_without_the_key_stops_with_a_plain_message(self) -> None:
        self.make_deb()

        for key in (None, ""):
            with self.subTest(key=key):
                result = self.run_script(key, release=True)

                self.assertEqual(1, result.returncode)
                self.assertIn(
                    "The signing key is not set up. The release owner adds it.",
                    result.stderr,
                )
        self.assertFalse(self.package_file.exists())

    def test_a_key_that_gpg_cannot_read_stops(self) -> None:
        self.make_deb()

        result = self.run_script("not a key")

        self.assertEqual(1, result.returncode)
        self.assertIn("gpg cannot read the signing key", result.stderr)
        self.assertNotIn(
            "not a key", result.stderr.replace("cannot read the signing key", "")
        )

    def test_a_folder_without_packages_stops(self) -> None:
        result = self.run_script(self.keys["test"][0])

        self.assertEqual(1, result.returncode)
        self.assertIn("There is no .deb file", result.stderr)

    def test_the_script_leaves_no_key_behind(self) -> None:
        self.make_deb()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)

        result = self.run_script(self.keys["test"][0], TMPDIR=str(tmp))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], list(tmp.iterdir()))


class UploadTests(ReleaseFilesCase):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "gcloud").write_text(STUB_GCLOUD)
        (self.bin / "gcloud").chmod(0o755)
        self.store = self.root / "store"
        (self.store / "release-files").mkdir(parents=True)
        self.log = self.root / "gcloud.log"
        self.release = self.root / "release"
        self.release.mkdir()
        self.write_release(b"ova one\n")

    def write_release(self, ova: bytes) -> None:
        (self.release / f"meho-appliance-{VERSION}.ova").write_bytes(ova)
        (self.release / f"meho-appliance-{VERSION}-os-packages.tar").write_bytes(
            b"packages\n"
        )
        lines = [
            f"{hashlib.sha256((self.release / name).read_bytes()).hexdigest()}  {name}\n"
            for name in (
                f"meho-appliance-{VERSION}.ova",
                f"meho-appliance-{VERSION}-os-packages.tar",
            )
        ]
        (self.release / "SHA256SUMS").write_text("".join(lines))
        (self.release / "SHA256SUMS.cosign.bundle").write_text(
            f"bundle for {len(ova)}\n"
        )

    def upload(
        self, bucket: str = "release-files", **extra: str
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STORE": str(self.store),
            "GCLOUD_LOG": str(self.log),
            "MEHO_RELEASE_BUCKET": bucket,
            **extra,
        }
        return subprocess.run(
            [str(UPLOAD), VERSION, str(self.release)],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip

    def folder(self) -> Path:
        return self.store / "release-files" / VERSION

    def test_the_four_files_land_in_the_folder_of_the_version(self) -> None:
        result = self.upload()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            sorted(
                [
                    f"meho-appliance-{VERSION}.ova",
                    f"meho-appliance-{VERSION}-os-packages.tar",
                    "SHA256SUMS",
                    "SHA256SUMS.cosign.bundle",
                ]
            ),
            sorted(path.name for path in self.folder().iterdir()),
        )
        for path in self.folder().iterdir():
            self.assertEqual((self.release / path.name).read_bytes(), path.read_bytes())

    def test_a_second_upload_with_the_same_content_changes_nothing(self) -> None:
        self.upload()
        before = {p.name: p.read_bytes() for p in self.folder().iterdir()}

        result = self.upload()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("already published with the same content", result.stdout)
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.folder().iterdir()}
        )

    def test_a_second_upload_with_other_content_is_refused(self) -> None:
        self.upload()
        before = {p.name: p.read_bytes() for p in self.folder().iterdir()}
        self.write_release(b"ova two, other content\n")

        result = self.upload()

        self.assertEqual(1, result.returncode)
        self.assertIn("already published with other content", result.stderr)
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.folder().iterdir()}
        )

    def test_an_upload_that_stopped_half_way_is_not_continued(self) -> None:
        self.folder().mkdir(parents=True)
        (self.folder() / f"meho-appliance-{VERSION}.ova").write_bytes(b"another ova\n")

        result = self.upload()

        self.assertEqual(1, result.returncode)
        self.assertIn("stopped half way", result.stderr)
        self.assertEqual(
            b"another ova\n",
            (self.folder() / f"meho-appliance-{VERSION}.ova").read_bytes(),
        )
        self.assertFalse((self.folder() / "SHA256SUMS").exists())

    def test_without_the_bucket_the_step_stops_with_a_plain_message(self) -> None:
        result = self.upload(bucket="")

        self.assertEqual(1, result.returncode)
        self.assertIn(
            "The bucket is not set up. The release owner creates it.", result.stderr
        )
        self.assertFalse(self.log.exists())

    def test_a_bucket_that_does_not_exist_stops_with_a_plain_message(
        self,
    ) -> None:
        result = self.upload(bucket="no-such-bucket")

        self.assertEqual(1, result.returncode)
        self.assertIn("The bucket gs://no-such-bucket does not exist", result.stderr)
        self.assertNotIn("[ok]", result.stdout)
        self.assertEqual([], [p for p in self.store.rglob("*") if p.is_file()])

    def test_another_gcloud_error_stops_without_its_text(self) -> None:
        result = self.upload(GCLOUD_FAIL="1")

        self.assertEqual(1, result.returncode)
        self.assertIn("[FAIL] gcloud could not read", result.stderr)
        self.assertNotIn("sa@example.invalid", result.stderr + result.stdout)
        self.assertNotIn("[ok]", result.stdout)
        self.assertEqual([], list(self.store.rglob("*.ova")))

    def test_a_file_skipped_by_no_clobber_is_not_reported_as_uploaded(
        self,
    ) -> None:
        result = self.upload(GCLOUD_SKIP="1")

        self.assertEqual(1, result.returncode)
        self.assertIn("was already there and was not replaced", result.stderr)
        self.assertNotIn("[ok]", result.stdout)

    def test_a_file_that_does_not_match_the_checksums_is_not_uploaded(self) -> None:
        (self.release / f"meho-appliance-{VERSION}.ova").write_bytes(b"changed\n")

        result = self.upload()

        self.assertEqual(1, result.returncode)
        self.assertIn("does not match SHA256SUMS", result.stderr)
        self.assertEqual([], list((self.store).rglob("*.ova")))

    def test_a_missing_file_stops_the_upload(self) -> None:
        (self.release / "SHA256SUMS.cosign.bundle").unlink()

        result = self.upload()

        self.assertEqual(1, result.returncode)
        self.assertIn("SHA256SUMS.cosign.bundle is missing", result.stderr)

    def test_the_script_never_creates_a_bucket_or_changes_access(self) -> None:
        self.upload()

        verbs = {line.split()[1] for line in self.log.read_text().splitlines()}
        self.assertEqual({"ls", "cp"}, verbs)


class GoogleCloudSignInTest(unittest.TestCase):
    """The job log is public and prints the inputs of each action. GitHub
    masks only secrets there, so the sign-in inputs must be secrets."""

    def test_the_sign_in_inputs_are_secrets(self) -> None:
        text = WORKFLOW.read_text()
        for name in ("workload_identity_provider", "service_account"):
            lines = [
                line.strip() for line in text.splitlines() if line.strip().startswith(f"{name}:")
            ]
            self.assertEqual(1, len(lines), name)
            self.assertRegex(lines[0], r"\$\{\{ secrets\.MEHO_RELEASE_GCP_[A-Z_]+ \}\}$")
        self.assertNotIn("vars.MEHO_RELEASE_GCP_", text)


if __name__ == "__main__":
    unittest.main()
