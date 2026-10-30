# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The start-up check of connector folders before MEHO imports them.

Each test builds a small, made-up install in ``tmp_path``: a package
``fakemeho_conn`` with a ``connectors`` folder, a made-up built-in
distribution that owns some of its subfolders, and (when needed) a
made-up connector package distribution plus an allowlist file. The
distributions are real ``.dist-info`` folders, so :mod:`importlib.metadata`
reads them exactly as it reads an installed wheel. The made-up
``connectors/__init__.py`` runs the check the same way the real one does.

The last test starts a fresh Python process on a copy of the real
``meho_backplane`` tree with a planted folder, and shows that importing
``meho_backplane.main`` stops before any connector code runs. One of its
cases plants a folder named like the check module itself, to show that
no folder under ``connectors/`` can take the place of the check.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

from meho_backplane import connector_packages
from meho_backplane.connector_packages import (
    ConnectorPackageError,
    check_connector_folders,
    load_connector_packages,
)
from meho_backplane.connectors.registry import (
    _eager_import_connectors,
    _import_connector_subpackages,
    registered_product_tokens,
)
from meho_backplane.operations import typed_register
from meho_backplane.version import version as version_route

_PKG = "fakemeho_conn"
_BUILTIN_DIST = "fake-meho-core"
_EXTRA_DIST = "fake-acme-connectors"

_CONNECTORS_INIT = (
    "from pathlib import Path\n"
    "from meho_backplane.connector_packages import check_connector_folders\n"
    "check_connector_folders(Path(next(iter(__path__))))\n"
)


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dist(
    site: Path,
    name: str,
    version: str,
    files: list[str],
    *,
    direct_url: dict[str, object] | None = None,
) -> None:
    """Write a ``.dist-info`` folder whose RECORD lists ``files``."""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    _write(info / "METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    record = [f"{f},," for f in files]
    if direct_url is not None:
        _write(info / "direct_url.json", json.dumps(direct_url))
    _write(info / "RECORD", "\n".join(record) + "\n")


_ACME_INIT = (
    "from meho_backplane.operations.typed_register import register_typed_op_registrar\n"
    "async def register_acme_widget_operations(*args, **kwargs):\n"
    "    return None\n"
    "register_typed_op_registrar(register_acme_widget_operations)\n"
)


class _LogRecorder:
    """Stands in for the module logger, so the test does not depend on structlog set-up."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append({"event": event, **fields})


@pytest.fixture(autouse=True)
def _keep_installed_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each check below stores what it accepted; put the process's own list back after."""
    monkeypatch.setattr(connector_packages, "_installed", connector_packages._installed)


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    recorder = _LogRecorder()
    monkeypatch.setattr(connector_packages, "_log", recorder)
    return recorder.events


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A made-up install: one built-in connector folder, on ``sys.path``."""
    site = tmp_path / "site"
    _write(site / _PKG / "__init__.py")
    _write(site / _PKG / "connectors" / "__init__.py", _CONNECTORS_INIT)
    _write(site / _PKG / "connectors" / "builtin_one" / "__init__.py", "LOADED = True\n")
    _write(site / _PKG / "connectors" / "builtin_one" / "ops.yaml", "ops: []\n")
    _dist(
        site,
        _BUILTIN_DIST,
        "1.0.0",
        [
            f"{_PKG}/__init__.py",
            f"{_PKG}/connectors/__init__.py",
            f"{_PKG}/connectors/builtin_one/__init__.py",
            f"{_PKG}/connectors/builtin_one/ops.yaml",
        ],
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(connector_packages, "BUILTIN_DISTRIBUTION", _BUILTIN_DIST)
    monkeypatch.setattr(
        connector_packages, "CONNECTOR_PACKAGES_FILE", tmp_path / "connector-packages.json"
    )
    registrars_before = list(typed_register._TYPED_OP_REGISTRARS)
    yield site
    typed_register._TYPED_OP_REGISTRARS[:] = registrars_before
    for mod in [m for m in sys.modules if m == _PKG or m.startswith(f"{_PKG}.")]:
        del sys.modules[mod]


def _connectors_pkg() -> ModuleType:
    """Import the made-up ``connectors`` package; its ``__init__`` runs the check."""
    return importlib.import_module(f"{_PKG}.connectors")


def _plant(site: Path, marker: Path) -> None:
    """Add a folder that is not built in. Importing it writes ``marker``."""
    _write(
        site / _PKG / "connectors" / "planted" / "__init__.py",
        f"open({str(marker)!r}, 'w').write('ran')\n",
    )


def _add_listed_package(site: Path, tmp_path: Path, *, version: str = "2.3.0") -> None:
    """Install a made-up connector package that adds the folder ``acme_widget``, and list it."""
    _write(site / _PKG / "connectors" / "acme_widget" / "__init__.py", _ACME_INIT)
    _dist(site, _EXTRA_DIST, "2.3.0", [f"{_PKG}/connectors/acme_widget/__init__.py"])
    _write(
        tmp_path / "connector-packages.json",
        json.dumps(
            {
                "packages": [
                    {
                        "name": _EXTRA_DIST,
                        "version": version,
                        "wheel_digest": "sha256:" + "0" * 64,
                        "folders": ["acme_widget"],
                    }
                ]
            }
        ),
    )


def test_unknown_folder_stops_the_start_and_nothing_is_imported(site: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    _plant(site, marker)

    with pytest.raises(ConnectorPackageError, match=r"connector_package_not_allowed.*'planted'"):
        _connectors_pkg()

    assert not marker.exists()
    assert f"{_PKG}.connectors" not in sys.modules
    assert f"{_PKG}.connectors.builtin_one" not in sys.modules
    assert f"{_PKG}.connectors.planted" not in sys.modules


def test_importing_one_connector_folder_directly_runs_the_check_first(
    site: Path, tmp_path: Path
) -> None:
    """A route module that imports one connector, as ``main.py`` does, is stopped the same way."""
    marker = tmp_path / "marker"
    _plant(site, marker)

    with pytest.raises(ConnectorPackageError, match=r"connector_package_not_allowed.*'planted'"):
        importlib.import_module(f"{_PKG}.connectors.builtin_one")

    assert not marker.exists()
    assert f"{_PKG}.connectors.builtin_one" not in sys.modules


def test_file_added_to_a_built_in_folder_stops_the_start(site: Path) -> None:
    _write(site / _PKG / "connectors" / "builtin_one" / "extra.py", "X = 1\n")

    with pytest.raises(
        ConnectorPackageError, match=r"connector_package_not_allowed.*'builtin_one'"
    ):
        _connectors_pkg()

    assert f"{_PKG}.connectors.builtin_one" not in sys.modules


def _link_folder_inside(site: Path, elsewhere: Path) -> None:
    (site / _PKG / "connectors" / "builtin_one" / "linked").symlink_to(
        elsewhere, target_is_directory=True
    )


def _link_file_inside(site: Path, elsewhere: Path) -> None:
    (site / _PKG / "connectors" / "builtin_one" / "linked.py").symlink_to(elsewhere / "extra.py")


def _link_the_folder_itself(site: Path, elsewhere: Path) -> None:
    folder = site / _PKG / "connectors" / "builtin_one"
    shutil.move(str(folder / "__init__.py"), str(elsewhere / "__init__.py"))
    shutil.move(str(folder / "ops.yaml"), str(elsewhere / "ops.yaml"))
    folder.rmdir()
    folder.symlink_to(elsewhere, target_is_directory=True)


@pytest.mark.parametrize(
    "link",
    [_link_folder_inside, _link_file_inside, _link_the_folder_itself],
    ids=["folder-link-inside", "file-link-inside", "folder-is-a-link"],
)
def test_symbolic_link_in_a_built_in_folder_stops_the_start(
    site: Path, tmp_path: Path, link: Callable[[Path, Path], None]
) -> None:
    """``os.walk`` does not enter a linked folder, so a link is never allowed."""
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "extra.py", "X = 1\n")
    link(site, elsewhere)

    with pytest.raises(
        ConnectorPackageError, match=r"connector_package_not_allowed.*'builtin_one'"
    ):
        _connectors_pkg()

    assert f"{_PKG}.connectors.builtin_one" not in sys.modules


def test_pycache_in_a_built_in_folder_is_ignored(site: Path) -> None:
    _write(
        site / _PKG / "connectors" / "builtin_one" / "__pycache__" / "__init__.cpython-314.pyc", "x"
    )

    _import_connector_subpackages(_connectors_pkg())

    assert f"{_PKG}.connectors.builtin_one" in sys.modules


def test_no_allowlist_file_imports_built_in_folders_only(
    site: Path, logs: list[dict[str, object]]
) -> None:
    _import_connector_subpackages(_connectors_pkg())

    assert sys.modules[f"{_PKG}.connectors.builtin_one"].LOADED is True
    allowed = [e for e in logs if e["event"] == "connector_packages_allowed"]
    assert allowed == [
        {
            "event": "connector_packages_allowed",
            "packages": [],
            "source_checkout": False,
        }
    ]


def test_listed_package_is_imported_and_registers_its_operations(
    site: Path, tmp_path: Path, logs: list[dict[str, object]]
) -> None:
    _add_listed_package(site, tmp_path)

    _import_connector_subpackages(_connectors_pkg())

    assert f"{_PKG}.connectors.acme_widget" in sys.modules
    assert f"{_PKG}.connectors.builtin_one" in sys.modules
    names = [r.__name__ for r in typed_register._TYPED_OP_REGISTRARS]
    assert "register_acme_widget_operations" in names
    allowed = next(e for e in logs if e["event"] == "connector_packages_allowed")
    assert allowed["packages"] == [
        {
            "name": _EXTRA_DIST,
            "version": "2.3.0",
            "wheel_digest": "sha256:" + "0" * 64,
            "folders": ["acme_widget"],
        }
    ]


async def test_version_lists_a_listed_package(site: Path, tmp_path: Path) -> None:
    """``GET /version`` shows the package the start-up check accepted (#5036)."""
    _add_listed_package(site, tmp_path)

    _connectors_pkg()

    body = (await version_route()).model_dump(mode="json")
    assert body["connector_packages"] == [
        {
            "name": _EXTRA_DIST,
            "version": "2.3.0",
            "wheel_digest": "sha256:" + "0" * 64,
            "folders": ["acme_widget"],
        }
    ]


async def test_version_lists_no_packages_without_an_allowlist(site: Path) -> None:
    """The plain public image has no allowlist file, so the list is empty (#5036)."""
    _connectors_pkg()

    assert (await version_route()).connector_packages == []


def test_listed_folder_with_a_file_the_package_did_not_install_stops_the_start(
    site: Path, tmp_path: Path
) -> None:
    _add_listed_package(site, tmp_path)
    _write(site / _PKG / "connectors" / "acme_widget" / "planted.py", "X = 1\n")

    with pytest.raises(
        ConnectorPackageError, match=r"connector_package_not_allowed.*'acme_widget'"
    ):
        _connectors_pkg()

    assert f"{_PKG}.connectors.acme_widget" not in sys.modules


def test_listed_version_must_match_the_installed_version(site: Path, tmp_path: Path) -> None:
    _add_listed_package(site, tmp_path, version="9.9.9")

    with pytest.raises(
        ConnectorPackageError, match=r"connector_package_list_invalid.*'9\.9\.9'.*'2\.3\.0'"
    ):
        _connectors_pkg()


def test_listed_package_that_is_not_installed_stops_the_start(site: Path, tmp_path: Path) -> None:
    _write(
        tmp_path / "connector-packages.json",
        json.dumps(
            {
                "packages": [
                    {
                        "name": "fake-missing",
                        "version": "1.0",
                        "wheel_digest": "sha256:0",
                        "folders": ["x"],
                    }
                ]
            }
        ),
    )

    with pytest.raises(
        ConnectorPackageError,
        match=r"connector_package_list_invalid.*'fake-missing'.*not installed",
    ):
        _connectors_pkg()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"packages": [{"name": "a", "version": "1", "folders": ["x"]}]}),
        json.dumps({"packages": [], "extra": 1}),
        json.dumps(
            {
                "packages": [
                    {"name": "a", "version": "1", "wheel_digest": "sha256:0", "folders": ["x"]},
                    {"name": "b", "version": "1", "wheel_digest": "sha256:0", "folders": ["x"]},
                ]
            }
        ),
        json.dumps(
            {"packages": [{"name": "a", "version": "1", "wheel_digest": "x", "folders": ["../x"]}]}
        ),
    ],
    ids=["not-json", "missing-digest", "unknown-field", "same-folder-twice", "folder-not-a-name"],
)
def test_bad_allowlist_file_stops_the_start(tmp_path: Path, content: str) -> None:
    path = tmp_path / "connector-packages.json"
    _write(path, content)

    with pytest.raises(ConnectorPackageError, match="connector_package_list_invalid"):
        load_connector_packages(path)


def test_missing_allowlist_file_means_no_extra_packages(tmp_path: Path) -> None:
    assert load_connector_packages(tmp_path / "absent.json") == []


def test_source_checkout_counts_every_folder_in_it_as_built_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[dict[str, object]]
) -> None:
    checkout = tmp_path / "checkout"
    connectors_dir = checkout / "src" / _PKG / "connectors"
    _write(connectors_dir / "local_dev" / "__init__.py")
    site = tmp_path / "site"
    _dist(
        site,
        "fake-meho-editable",
        "0.1.0",
        ["_editable_fake.pth"],
        direct_url={"url": checkout.as_uri(), "dir_info": {"editable": True}},
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(connector_packages, "BUILTIN_DISTRIBUTION", "fake-meho-editable")

    check_connector_folders(connectors_dir, tmp_path / "absent.json")

    assert (
        next(e for e in logs if e["event"] == "connector_packages_allowed")["source_checkout"]
        is True
    )


def test_real_connector_walk_starts_as_today_without_an_allowlist(
    tmp_path: Path, logs: list[dict[str, object]]
) -> None:
    import meho_backplane.connectors as real

    check_connector_folders(Path(next(iter(real.__path__))), tmp_path / "absent.json")
    _eager_import_connectors()

    assert [e["event"] for e in logs] == ["connector_packages_allowed"]
    assert "vault" in registered_product_tokens()


_FRESH_START = """\
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
try:
    import meho_backplane.main
except Exception as exc:  # noqa: BLE001
    loaded = sorted(
        m
        for m in sys.modules
        if m == "meho_backplane.connectors" or m.startswith("meho_backplane.connectors.")
    )
    guard = sys.modules.get("meho_backplane.connector_packages")
    print("STOPPED:", exc)
    print("LOADED:", " ".join(loaded))
    print("GUARD:", "" if guard is None else guard.__file__)
    sys.exit(3)
print("STARTED")
"""


@pytest.mark.parametrize("folder", ["zz_planted", "packages"], ids=["any-name", "check-name"])
def test_fresh_process_rejects_a_planted_folder_before_any_connector_code_runs(
    tmp_path: Path, folder: str
) -> None:
    """Start a new interpreter on a copy of the real tree, installed like a wheel.

    ``meho_backplane.main`` imports connector code through its route modules
    long before its lifespan runs. The copy carries a real ``.dist-info``
    whose file list names every file but the planted folder, so the check
    sees it as a wheel install. The import must stop on the planted folder,
    and no ``connectors.<folder>`` module may have been loaded.

    The second case plants a folder named ``packages``, the old name of
    the check module inside ``connectors/``. Python prefers a folder over
    a module file of the same name, so a check that lived there would be
    replaced by the planted code. The check lives outside the tree, so
    the planted code must never run, and the check that stops the start
    must be the real one.
    """
    real_pkg = Path(connector_packages.__file__).resolve().parent
    src = tmp_path / "src"
    copy = src / "meho_backplane"
    shutil.copytree(real_pkg, copy, ignore=shutil.ignore_patterns("__pycache__"))
    marker = tmp_path / "marker"
    _write(
        copy / "connectors" / folder / "__init__.py",
        f"open({str(marker)!r}, 'w')\ndef check_connector_folders(*args, **kwargs):\n    pass\n",
    )
    installed = [
        p.relative_to(src).as_posix()
        for p in sorted(copy.rglob("*"))
        if p.is_file() and folder not in p.parts
    ]
    _dist(src, connector_packages.BUILTIN_DISTRIBUTION, "0.0.0+test", installed)
    script = tmp_path / "fresh_start.py"
    script.write_text(_FRESH_START, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(src)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "STOPPED: connector_package_not_allowed" in result.stdout
    assert f"{folder!r}" in result.stdout
    assert "LOADED: \n" in result.stdout
    assert f"GUARD: {copy / 'connector_packages.py'}\n" in result.stdout
    assert not marker.exists()
