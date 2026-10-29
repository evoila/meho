# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the checks of the connector overlay image build.

The checks live in ``deploy/connector-overlay/overlay_check.py``. They run
inside the image build, with only the standard library. These tests load
that file, write small made-up wheels, and run the checks against a
throwaway virtual environment.

The tests that install wheels use ``uv`` (offline, from a local folder),
the same way the image build does.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

from meho_backplane.connector_packages import load_connector_packages

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "connector-overlay" / "overlay_check.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("overlay_check", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["overlay_check"] = module
    spec.loader.exec_module(module)
    return module


oc = _load()

_UV = shutil.which("uv")
needs_uv = pytest.mark.skipif(_UV is None, reason="uv is not on PATH")


_WHEEL_FILE = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def _hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_wheel(
    folder: Path,
    name: str,
    version: str,
    files: dict[str, str],
    *,
    requires: tuple[str, ...] = (),
    tamper: str | None = None,
) -> Path:
    """Write a small wheel file with a correct RECORD (unless ``tamper`` names a file)."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{name.replace('-', '_')}-{version}"
    dist_info = f"{stem}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {r}\n" for r in requires)
    content = {
        **files,
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": _WHEEL_FILE,
    }
    record = [
        f"{path},{_hash(text.encode())},{len(text.encode())}" for path, text in content.items()
    ]
    record.append(f"{dist_info}/RECORD,,")
    path = folder / f"{stem}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for entry, text in content.items():
            zf.writestr(entry, text + ("# changed\n" if entry == tamper else ""))
        zf.writestr(f"{dist_info}/RECORD", "\n".join(record) + "\n")
    return path


def _connector_files(folder: str = "example_widget") -> dict[str, str]:
    return {f"meho_backplane/connectors/{folder}/__init__.py": "VALUE = 1\n"}


# ---------------------------------------------------------------------------
# Wheel checks before install
# ---------------------------------------------------------------------------


def test_connector_wheel_is_read(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, "example-connectors", "1.2.0", _connector_files())
    wheel = oc.read_wheel(path, connector=True)
    assert (wheel.name, wheel.version, wheel.folders) == (
        "example-connectors",
        "1.2.0",
        ("example_widget",),
    )
    assert wheel.digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_wheel_with_pth_file_is_refused(tmp_path: Path) -> None:
    path = make_wheel(
        tmp_path, "example-connectors", "1.0", {**_connector_files(), "hook.pth": "import os\n"}
    )
    with pytest.raises(oc.BuildError, match=r"hook\.pth is a start-up hook"):
        oc.read_wheel(path, connector=True)


def test_extra_wheel_with_sitecustomize_is_refused(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, "helper-lib", "1.0", {"sitecustomize.py": "x = 1\n"})
    with pytest.raises(oc.BuildError, match="start-up hook"):
        oc.read_wheel(path, connector=False)


@pytest.mark.parametrize(
    "entry",
    [
        "sitecustomize/__init__.py",
        "sitecustomize/helper.py",
        "usercustomize.pyc",
        "sitecustomize.cpython-312-x86_64-linux-gnu.so",
        "usercustomize.abi3.so",
    ],
)
def test_extra_wheel_with_another_form_of_sitecustomize_is_refused(
    tmp_path: Path, entry: str
) -> None:
    path = make_wheel(tmp_path, "helper-lib", "1.0", {entry: "x = 1\n"})
    with pytest.raises(oc.BuildError, match="start-up hook"):
        oc.read_wheel(path, connector=False)


def test_extra_wheel_with_a_name_that_only_looks_like_a_hook_is_read(tmp_path: Path) -> None:
    files = {"sitecustomize_tools/__init__.py": "", "helper_lib/sitecustomize.py": "x = 1\n"}
    path = make_wheel(tmp_path, "helper-lib", "1.0", files)
    assert oc.read_wheel(path, connector=False).name == "helper-lib"


def test_extra_wheel_with_pth_in_data_folder_is_refused(tmp_path: Path) -> None:
    path = make_wheel(
        tmp_path, "helper-lib", "1.0", {"helper_lib-1.0.data/purelib/zz.pth": "import os\n"}
    )
    with pytest.raises(oc.BuildError, match="start-up hook"):
        oc.read_wheel(path, connector=False)


def test_extra_wheel_with_data_files_is_refused(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, "helper-lib", "1.0", {"helper_lib-1.0.data/scripts/uvicorn": "x\n"})
    with pytest.raises(oc.BuildError, match="outside the package folder"):
        oc.read_wheel(path, connector=False)


def test_connector_wheel_writing_core_files_is_refused(tmp_path: Path) -> None:
    path = make_wheel(
        tmp_path,
        "example-connectors",
        "1.0",
        {**_connector_files(), "meho_backplane/main.py": "app = None\n"},
    )
    with pytest.raises(oc.BuildError, match="outside its own connector folder"):
        oc.read_wheel(path, connector=True)


def test_connector_wheel_writing_a_top_level_package_is_refused(tmp_path: Path) -> None:
    path = make_wheel(
        tmp_path, "example-connectors", "1.0", {**_connector_files(), "other/__init__.py": ""}
    )
    with pytest.raises(oc.BuildError, match="outside its own connector folder"):
        oc.read_wheel(path, connector=True)


def test_extra_wheel_writing_core_files_is_refused(tmp_path: Path) -> None:
    path = make_wheel(tmp_path, "helper-lib", "1.0", {"meho_backplane/auth.py": "x = 1\n"})
    with pytest.raises(oc.BuildError, match="into MEHO's own files"):
        oc.read_wheel(path, connector=False)


def test_changed_wheel_is_refused(tmp_path: Path) -> None:
    files = _connector_files()
    path = make_wheel(tmp_path, "example-connectors", "1.0", files, tamper=next(iter(files)))
    with pytest.raises(oc.BuildError, match="does not match its RECORD"):
        oc.read_wheel(path, connector=True)


def test_two_connector_wheels_with_the_same_folder_are_refused(tmp_path: Path) -> None:
    make_wheel(tmp_path, "first-connectors", "1.0", _connector_files("shared"))
    make_wheel(tmp_path, "second-connectors", "1.0", _connector_files("shared"))
    with pytest.raises(oc.BuildError, match="'shared' is in both"):
        oc.check_wheels(tmp_path, None)


# ---------------------------------------------------------------------------
# Checks after install, on recorded file lists
# ---------------------------------------------------------------------------

_SITE = "lib/python3.12/site-packages"
_BASE_FILES = (
    "meho_backplane/__init__.py",
    "meho_backplane/connectors/__init__.py",
    "meho_backplane/connectors/registry.py",
    "meho_backplane/connectors/builtin_one/__init__.py",
    "fakelib/__init__.py",
    "flatlib.py",
    "fastmod.cpython-312-x86_64-linux-gnu.so",
)


def _state(*files: str) -> dict[str, list[int]]:
    return {f"{_SITE}/{name}": [1, 1, i] for i, name in enumerate(files)}


def _check_added(*files: str, folders: set[str] | None = None) -> list[str]:
    before = _state(*_BASE_FILES)
    after = {**before, **_state(*files)}
    new: list[str] = oc.check_files(before, after, PurePosixPath(_SITE), folders or set())
    return new


@pytest.mark.parametrize(
    "entry",
    [
        "sitecustomize/__init__.py",
        "sitecustomize/helper.py",
        "usercustomize.pyc",
        "sitecustomize.cpython-312-x86_64-linux-gnu.so",
        "zz.pth",
    ],
)
def test_installed_startup_hook_is_refused(entry: str) -> None:
    with pytest.raises(oc.BuildError, match=rf"{re.escape(entry)} \(is a start-up hook\)"):
        _check_added(entry)


@pytest.mark.parametrize(
    "entry",
    [
        "flatlib/__init__.py",
        "fakelib.py",
        "fakelib.pyc",
        "fakelib.cpython-312-x86_64-linux-gnu.so",
        "fastmod/__init__.py",
        "fastmod.py",
    ],
)
def test_installed_module_with_the_name_of_a_base_module_is_refused(entry: str) -> None:
    with pytest.raises(oc.BuildError, match=rf"{re.escape(entry)} \(takes the place of a module"):
        _check_added(entry)


def test_installed_connector_folder_with_the_name_of_a_base_module_is_refused() -> None:
    with pytest.raises(oc.BuildError, match=r"\['registry'\] already exist"):
        _check_added("meho_backplane/connectors/registry/__init__.py", folders={"registry"})


def test_installed_new_modules_are_allowed() -> None:
    new = _check_added(
        "helper_lib/__init__.py",
        "flatlib_extra.py",
        "fakelib_tools/__init__.py",
        "meho_backplane/connectors/example_widget/__init__.py",
        folders={"example_widget"},
    )
    assert len(new) == 4


# ---------------------------------------------------------------------------
# Checks after install, with a real install by uv
# ---------------------------------------------------------------------------


def _uv(*args: str) -> subprocess.CompletedProcess[str]:
    assert _UV is not None
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "VIRTUAL_ENV"))}
    env.update(UV_NO_CACHE="1", UV_PYTHON_DOWNLOADS="never", UV_LINK_MODE="copy")
    return subprocess.run([_UV, *args], env=env, capture_output=True, text=True, check=False)


def _install(venv: Path, wheels: list[Path], *find_links: Path) -> None:
    links = [arg for folder in find_links for arg in ("--find-links", str(folder))]
    result = _uv(
        "pip",
        "install",
        "--python",
        str(venv / "bin" / "python"),
        "--offline",
        "--no-index",
        "--no-build",
        *links,
        *map(str, wheels),
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def base_venv(tmp_path: Path) -> Path:
    """A throwaway environment that stands in for the base image's ``/app/.venv``."""
    venv = tmp_path / "venv"
    result = _uv("venv", "--python", sys.executable, str(venv))
    assert result.returncode == 0, result.stderr
    base = tmp_path / "base-wheels"
    _install(
        venv,
        [
            make_wheel(
                base,
                "meho-backplane",
                "0.0.1",
                {
                    "meho_backplane/__init__.py": "",
                    "meho_backplane/connectors/__init__.py": "",
                    "meho_backplane/connectors/registry.py": "",
                    "meho_backplane/connectors/builtin_one/__init__.py": "",
                },
                requires=("fakelib==1.0",),
            ),
            make_wheel(base, "fakelib", "1.0", {"fakelib/__init__.py": "V = 1\n"}),
            make_wheel(base, "flat-lib", "1.0", {"flatlib.py": "F = 1\n"}),
        ],
    )
    return venv


def _build(venv: Path, connectors: Path, deps: Path, out: Path) -> list[dict[str, object]]:
    """The build steps of the Dockerfile: check wheels, snapshot, install, finish."""
    deps.mkdir(exist_ok=True)
    oc.check_wheels(connectors, deps)
    before = out.parent / "before.json"
    before.write_text(json.dumps(oc.snapshot(venv)))
    _install(venv, sorted(connectors.glob("*.whl")), connectors, deps)
    return oc.finish(venv, before, connectors, out)


@needs_uv
def test_allowlist_matches_the_installed_wheels(base_venv: Path, tmp_path: Path) -> None:
    connectors, deps, out = tmp_path / "connectors", tmp_path / "deps", tmp_path / "out"
    first = make_wheel(
        connectors,
        "example-connectors",
        "1.2.0",
        {
            "meho_backplane/connectors/example_widget/__init__.py": "A = 1\n",
            "meho_backplane/connectors/example_gadget/__init__.py": "B = 1\n",
        },
        requires=("helper-lib>=1", "fakelib==1.0"),
    )
    second = make_wheel(connectors, "other-connectors", "0.3", _connector_files("other_thing"))
    make_wheel(deps, "helper-lib", "1.1", {"helper_lib/__init__.py": "H = 1\n"})

    _build(base_venv, connectors, deps, out)

    allowlist = out / "app" / "connector-packages.json"
    packages = load_connector_packages(allowlist)
    by_name = {p.name: p for p in packages}
    assert set(by_name) == {"example-connectors", "other-connectors"}
    for wheel, folders in (
        (first, ["example_gadget", "example_widget"]),
        (second, ["other_thing"]),
    ):
        entry = by_name[oc.read_wheel(wheel, connector=True).name]
        assert entry.wheel_digest == "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
        assert entry.folders == folders
    installed = oc.installed_versions(oc.site_packages(base_venv))
    assert installed["example-connectors"][1] == by_name["example-connectors"].version
    assert installed["other-connectors"][1] == by_name["other-connectors"].version

    copied = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    site = oc.site_packages(base_venv).relative_to(base_venv.anchor).as_posix()
    assert f"{site}/meho_backplane/connectors/example_widget/__init__.py" in copied
    assert f"{site}/helper_lib/__init__.py" in copied
    assert f"{site}/fakelib/__init__.py" not in copied
    assert f"{site}/meho_backplane/connectors/builtin_one/__init__.py" not in copied


@needs_uv
def test_clashing_library_fails_and_names_it(base_venv: Path, tmp_path: Path) -> None:
    connectors, deps, out = tmp_path / "connectors", tmp_path / "deps", tmp_path / "out"
    make_wheel(
        connectors, "example-connectors", "1.0", _connector_files(), requires=("fakelib==2.0",)
    )
    make_wheel(deps, "fakelib", "2.0", {"fakelib/__init__.py": "V = 2\n"})
    with pytest.raises(oc.BuildError, match=r"Changed: fakelib 1\.0 -> 2\.0"):
        _build(base_venv, connectors, deps, out)
    assert not (out / "app" / "connector-packages.json").exists()


@needs_uv
def test_extra_wheel_adding_files_to_a_base_package_is_refused(
    base_venv: Path, tmp_path: Path
) -> None:
    connectors, deps, out = tmp_path / "connectors", tmp_path / "deps", tmp_path / "out"
    make_wheel(
        connectors, "example-connectors", "1.0", _connector_files(), requires=("sneaky-lib",)
    )
    make_wheel(deps, "sneaky-lib", "1.0", {"fakelib/extra.py": "x = 1\n"})
    with pytest.raises(oc.BuildError, match=r"fakelib/extra\.py \(is a new file in a folder"):
        _build(base_venv, connectors, deps, out)


@needs_uv
def test_extra_wheel_replacing_a_base_file_is_refused(base_venv: Path, tmp_path: Path) -> None:
    connectors, deps, out = tmp_path / "connectors", tmp_path / "deps", tmp_path / "out"
    make_wheel(
        connectors, "example-connectors", "1.0", _connector_files(), requires=("sneaky-lib",)
    )
    make_wheel(deps, "sneaky-lib", "1.0", {"fakelib/__init__.py": "V = 666\n"})
    with pytest.raises(oc.BuildError, match=r"changed or removed files of the base image"):
        _build(base_venv, connectors, deps, out)


@needs_uv
def test_connector_folder_that_is_built_in_is_refused(base_venv: Path, tmp_path: Path) -> None:
    connectors, deps, out = tmp_path / "connectors", tmp_path / "deps", tmp_path / "out"
    make_wheel(
        connectors,
        "example-connectors",
        "1.0",
        {
            "meho_backplane/connectors/builtin_one/extra.py": "x = 1\n",
        },
    )
    with pytest.raises(oc.BuildError, match=r"\['builtin_one'\] already exist"):
        _build(base_venv, connectors, deps, out)


@needs_uv
def test_connector_folder_named_like_a_base_module_is_refused(
    base_venv: Path, tmp_path: Path
) -> None:
    connectors, deps, out = tmp_path / "connectors", tmp_path / "deps", tmp_path / "out"
    make_wheel(connectors, "example-connectors", "1.0", _connector_files("registry"))
    with pytest.raises(oc.BuildError, match=r"\['registry'\] already exist"):
        _build(base_venv, connectors, deps, out)
    assert not (out / "app" / "connector-packages.json").exists()


@needs_uv
def test_extra_wheel_package_named_like_a_base_module_is_refused(
    base_venv: Path, tmp_path: Path
) -> None:
    connectors, deps, out = tmp_path / "connectors", tmp_path / "deps", tmp_path / "out"
    make_wheel(
        connectors, "example-connectors", "1.0", _connector_files(), requires=("sneaky-lib",)
    )
    make_wheel(deps, "sneaky-lib", "1.0", {"flatlib/__init__.py": "F = 666\n"})
    with pytest.raises(oc.BuildError, match=r"flatlib/__init__\.py \(takes the place of a module"):
        _build(base_venv, connectors, deps, out)


def test_main_reports_a_refused_wheel(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    make_wheel(tmp_path, "example-connectors", "1.0", {**_connector_files(), "x.pth": "import os"})
    assert oc.main(["check-wheels", "--connectors", str(tmp_path)]) == 1
    assert "connector overlay build refused" in capfd.readouterr().err
