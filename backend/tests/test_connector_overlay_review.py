# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the review records that signing a connector overlay image needs.

An image that evoila signs for the appliance (tier 2) may only hold
connector wheels that a person reviewed. ``overlay_check.py check-reviews``
checks that each wheel has a review record for its exact name, version and
wheel digest. ``build.sh --sign`` and ``--sign-key`` run it before the build
starts. The build.sh tests use stand-ins for ``docker`` and ``cosign``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_connector_overlay_build import _connector_files, make_wheel, oc

_BUILD_SH = Path(__file__).resolve().parents[2] / "deploy" / "connector-overlay" / "build.sh"
_BASE = "ghcr.io/example/meho@sha256:" + "a" * 64
_PUSHED = "sha256:" + "b" * 64

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not on PATH")


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _record(folder: Path, wheel: Path, name: str, version: str, **change: object) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "name": name,
        "version": version,
        "wheel_digest": _digest(wheel),
        "reviewer": "Jane Example",
        "date": "2026-10-30",
        "checklist": dict.fromkeys(oc.REVIEW_CHECKLIST, True),
    }
    data.update(change)
    path = folder / f"{name}-{version}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def wheel(tmp_path: Path) -> Path:
    return make_wheel(tmp_path / "connectors", "example-connectors", "1.2.0", _connector_files())


# ---------------------------------------------------------------------------
# check-reviews
# ---------------------------------------------------------------------------


def test_review_for_the_exact_wheel_passes(tmp_path: Path, wheel: Path) -> None:
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.2.0")
    [(checked, review)] = oc.check_reviews(wheel.parent, tmp_path / "reviews")
    assert (checked.name, review.reviewer, review.wheel_digest) == (
        "example-connectors",
        "Jane Example",
        _digest(wheel),
    )


def test_no_review_record_stops(tmp_path: Path, wheel: Path) -> None:
    (tmp_path / "reviews").mkdir()
    with pytest.raises(oc.BuildError, match=r"no review record for example-connectors 1\.2\.0"):
        oc.check_reviews(wheel.parent, tmp_path / "reviews")


def test_changed_wheel_needs_a_new_review(tmp_path: Path, wheel: Path) -> None:
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.2.0")
    changed = make_wheel(
        tmp_path / "connectors",
        "example-connectors",
        "1.2.0",
        _connector_files() | {"meho_backplane/connectors/example_widget/extra.py": "X = 2\n"},
    )
    assert changed == wheel
    with pytest.raises(oc.BuildError, match="The wheel changed, so a person must review it again"):
        oc.check_reviews(wheel.parent, tmp_path / "reviews")


def test_review_of_another_version_does_not_count(tmp_path: Path, wheel: Path) -> None:
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.1.0")
    with pytest.raises(oc.BuildError, match="no review record"):
        oc.check_reviews(wheel.parent, tmp_path / "reviews")


def test_package_name_is_compared_in_its_normal_form(tmp_path: Path, wheel: Path) -> None:
    _record(tmp_path / "reviews", wheel, "Example_Connectors", "1.2.0")
    assert len(oc.check_reviews(wheel.parent, tmp_path / "reviews")) == 1


def test_review_by_a_bot_is_refused(tmp_path: Path, wheel: Path) -> None:
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.2.0", reviewer="helper[bot]")
    with pytest.raises(oc.BuildError, match=r"is a bot\. A person must review"):
        oc.check_reviews(wheel.parent, tmp_path / "reviews")


def test_review_with_an_open_checklist_point_is_refused(tmp_path: Path, wheel: Path) -> None:
    checklist = dict.fromkeys(oc.REVIEW_CHECKLIST, True) | {"licences": False}
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.2.0", checklist=checklist)
    with pytest.raises(oc.BuildError, match="did not confirm: licences"):
        oc.check_reviews(wheel.parent, tmp_path / "reviews")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"reviewer": " "}, "has no 'reviewer'"),
        ({"date": "30.10.2026"}, "date must be YYYY-MM-DD"),
        ({"wheel_digest": "sha256:abc"}, "wheel_digest must be"),
        ({"checklist": None}, "did not confirm: operation_contracts"),
    ],
)
def test_incomplete_review_record_is_refused(
    tmp_path: Path, wheel: Path, change: dict[str, object], message: str
) -> None:
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.2.0", **change)
    with pytest.raises(oc.BuildError, match=message):
        oc.check_reviews(wheel.parent, tmp_path / "reviews")


def test_main_reports_a_missing_review(
    tmp_path: Path, wheel: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "reviews").mkdir()
    reviews = str(tmp_path / "reviews")
    args = ["check-reviews", "--connectors", str(wheel.parent), "--reviews", reviews]
    assert oc.main(args) == 1
    assert "connector overlay signing refused: no review record" in capfd.readouterr().err


# ---------------------------------------------------------------------------
# build.sh --sign
# ---------------------------------------------------------------------------


def _stand_ins(folder: Path) -> Path:
    """Write ``docker`` and ``cosign`` stand-ins that log their arguments."""
    folder.mkdir()
    (folder / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'echo "docker $*" >> "$STUB_LOG"\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = --metadata-file ]; then\n'
        f'    echo \'{{"containerimage.digest": "{_PUSHED}"}}\' > "$2"\n'
        "  fi\n"
        "  shift\n"
        "done\n",
        encoding="utf-8",
    )
    (folder / "cosign").write_text(
        "#!/usr/bin/env bash\n"
        'echo "cosign $*" >> "$STUB_LOG"\n'
        'if [ -n "${COSIGN_PASSWORD:-}" ]; then echo "cosign got a password" >> "$STUB_LOG"; fi\n',
        encoding="utf-8",
    )
    for tool in ("docker", "cosign"):
        (folder / tool).chmod(0o755)
    return folder


def _build(
    tmp_path: Path, wheel: Path, *flags: str, **extra_env: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{_stand_ins(tmp_path / 'bin')}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        **extra_env,
    }
    args = ["--base", _BASE, "--connectors", str(wheel.parent), "--tag", "registry.example/o:1"]
    result = subprocess.run(
        ["bash", str(_BUILD_SH), *args, *flags],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log.read_text(encoding="utf-8")


def _sign(tmp_path: Path, wheel: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], str]:
    return _build(tmp_path, wheel, "--push", "--sign", *extra)


@needs_bash
def test_sign_without_reviews_folder_stops(tmp_path: Path, wheel: Path) -> None:
    result, calls = _sign(tmp_path, wheel)
    assert result.returncode == 1
    assert "--sign needs --reviews" in result.stderr
    assert calls == ""


@needs_bash
def test_sign_without_a_record_for_the_wheel_stops_before_the_build(
    tmp_path: Path, wheel: Path
) -> None:
    (tmp_path / "reviews").mkdir()
    result, calls = _sign(tmp_path, wheel, "--reviews", str(tmp_path / "reviews"))
    assert result.returncode == 1
    assert "no review record for example-connectors 1.2.0" in result.stderr
    assert "signing stopped" in result.stderr
    assert calls == ""


@needs_bash
def test_sign_with_a_record_for_the_exact_wheel_builds_and_signs(
    tmp_path: Path, wheel: Path
) -> None:
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.2.0")
    result, calls = _sign(tmp_path, wheel, "--reviews", str(tmp_path / "reviews"))
    assert result.returncode == 0, result.stderr
    assert "review ok: example-connectors 1.2.0" in result.stdout
    assert "docker buildx build" in calls
    assert f"cosign sign --yes registry.example/o@{_PUSHED}" in calls


@needs_bash
def test_unknown_option_stops_before_the_build(tmp_path: Path, wheel: Path) -> None:
    result, calls = _build(tmp_path, wheel, "--push", "--no-such-option", "x")
    assert result.returncode == 1
    assert "unknown argument: --no-such-option" in result.stderr
    assert calls == ""


# ---------------------------------------------------------------------------
# build.sh --sign-key
# ---------------------------------------------------------------------------


def _key_and_reviews(tmp_path: Path, wheel: Path) -> tuple[str, str]:
    _record(tmp_path / "reviews", wheel, "example-connectors", "1.2.0")
    key = tmp_path / "test.key"
    key.touch()
    return str(key), str(tmp_path / "reviews")


@needs_bash
@pytest.mark.parametrize("sign_too", [(), ("--sign",)], ids=["key alone", "key with --sign"])
def test_sign_key_signs_with_that_key_and_no_log(
    tmp_path: Path, wheel: Path, sign_too: tuple[str, ...]
) -> None:
    key, reviews = _key_and_reviews(tmp_path, wheel)
    result, calls = _build(
        tmp_path, wheel, "--push", *sign_too, "--sign-key", key, "--reviews", reviews
    )
    assert result.returncode == 0, result.stderr
    assert (
        f"cosign sign --yes --key {key} --tlog-upload=false --use-signing-config=false "
        f"registry.example/o@{_PUSHED}\n"
    ) in calls
    assert f"signed: registry.example/o@{_PUSHED}" in result.stdout


@needs_bash
def test_sign_key_takes_a_key_reference_that_is_not_a_file(tmp_path: Path, wheel: Path) -> None:
    _, reviews = _key_and_reviews(tmp_path, wheel)
    result, calls = _build(
        tmp_path, wheel, "--push", "--sign-key", "hashivault://overlay", "--reviews", reviews
    )
    assert result.returncode == 0, result.stderr
    assert "cosign sign --yes --key hashivault://overlay " in calls


@needs_bash
def test_sign_key_passes_the_password_only_by_the_environment(tmp_path: Path, wheel: Path) -> None:
    password = "made-up-test-password"
    key, reviews = _key_and_reviews(tmp_path, wheel)
    result, calls = _build(
        tmp_path,
        wheel,
        "--push",
        "--sign-key",
        key,
        "--reviews",
        reviews,
        COSIGN_PASSWORD=password,
    )
    assert result.returncode == 0, result.stderr
    assert "cosign got a password" in calls
    assert password not in calls + result.stdout + result.stderr


@needs_bash
def test_sign_key_needs_push(tmp_path: Path, wheel: Path) -> None:
    key, reviews = _key_and_reviews(tmp_path, wheel)
    result, calls = _build(tmp_path, wheel, "--sign-key", key, "--reviews", reviews)
    assert result.returncode == 1
    assert "--sign-key needs --push" in result.stderr
    assert calls == ""


@needs_bash
def test_sign_key_needs_reviews(tmp_path: Path, wheel: Path) -> None:
    key, _ = _key_and_reviews(tmp_path, wheel)
    result, calls = _build(tmp_path, wheel, "--push", "--sign-key", key)
    assert result.returncode == 1
    assert "--sign-key needs --reviews" in result.stderr
    assert calls == ""


@needs_bash
@pytest.mark.parametrize(
    "key_flags",
    [("--sign-key", ""), ("--sign-key", "--push"), ("--sign-key",)],
    ids=["empty", "another option", "nothing after it"],
)
def test_sign_key_without_a_key_stops_and_never_signs(
    tmp_path: Path, wheel: Path, key_flags: tuple[str, ...]
) -> None:
    _, reviews = _key_and_reviews(tmp_path, wheel)
    result, calls = _build(tmp_path, wheel, "--push", "--sign", "--reviews", reviews, *key_flags)
    assert result.returncode == 1
    assert "--sign-key needs a key reference" in result.stderr
    assert calls == ""


@needs_bash
def test_sign_key_with_a_missing_key_file_stops_before_the_build(
    tmp_path: Path, wheel: Path
) -> None:
    _, reviews = _key_and_reviews(tmp_path, wheel)
    missing = str(tmp_path / "missing.key")
    result, calls = _build(tmp_path, wheel, "--push", "--sign-key", missing, "--reviews", reviews)
    assert result.returncode == 1
    assert f"--sign-key: no key file at {missing}" in result.stderr
    assert calls == ""


@needs_bash
def test_sign_key_without_a_record_for_the_wheel_stops_before_the_build(
    tmp_path: Path, wheel: Path
) -> None:
    key, _ = _key_and_reviews(tmp_path, wheel)
    (tmp_path / "no-reviews").mkdir()
    result, calls = _build(
        tmp_path, wheel, "--push", "--sign-key", key, "--reviews", str(tmp_path / "no-reviews")
    )
    assert result.returncode == 1
    assert "no review record for example-connectors 1.2.0" in result.stderr
    assert calls == ""


@needs_bash
def test_help_shows_the_whole_usage_text() -> None:
    result = subprocess.run(
        ["bash", str(_BUILD_SH), "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "--sign-key" in result.stdout
    assert result.stdout.rstrip().endswith("every wheel must be in the two folders.")
