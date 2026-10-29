# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks for the build of a connector overlay image.

An overlay image is a new image built from the public MEHO image, with
connector packages (wheels) added. The Dockerfile next to this file runs
three steps in a throwaway build stage:

1. ``check-wheels``: read every wheel file before anything is installed.
   Refuse a wheel whose file list does not match its own ``RECORD``, a
   wheel with a start-up hook (a ``.pth`` file, or any importable form of
   ``sitecustomize`` or ``usercustomize``: a module file, a compiled file,
   an extension module or a package folder), a wheel with a compiled-file
   cache (``__pycache__``) above its own folder, a connector wheel with
   files outside its own connector folders, and an extra wheel with files
   in MEHO's own package.
2. ``snapshot``: record every file and every installed package version of
   the base image's environment. The base image installs exactly the
   versions of its lock file, so this is the lock to check against.
3. ``finish``: after ``uv pip install``, compare the environment with the
   snapshot. Refuse any library whose version changed, any base file that
   changed or went away, any new file that is not in a new folder, any
   new module or package whose name an existing one already has (Python
   would import the new one instead), and any new compiled-file cache next
   to modules of the base image (Python could load it instead of their
   source).
   Then copy only the new files to an output folder and write the
   allowlist file (``/app/connector-packages.json``) that MEHO checks at
   start.

Only the standard library is used. Run it with ``python -I -S`` so that no
``.pth`` file of the environment being checked can run code in this process.

See ``docs/codebase/connector-packages.md``.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from email.parser import HeaderParser
from pathlib import Path, PurePosixPath

CONNECTORS_PREFIX = ("meho_backplane", "connectors")
ALLOWLIST_PATH = PurePosixPath("/app/connector-packages.json")
STARTUP_HOOK_MODULES = frozenset({"sitecustomize", "usercustomize"})
PYCACHE = "__pycache__"
# The file endings Python imports a module from: source, compiled, and extension modules.
_MODULE_SUFFIXES = (".py", ".pyc", ".pyw", ".pyd", ".so")
_FOLDER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RECORD_SIGNATURES = frozenset({"RECORD.jws", "RECORD.p7s"})
_MAX_NAMED = 10


class BuildError(Exception):
    """The overlay build must stop. The message says why."""


@dataclass(frozen=True)
class Wheel:
    """One wheel file, as read before install."""

    path: Path
    name: str
    version: str
    digest: str
    folders: tuple[str, ...]


def canonical_name(name: str) -> str:
    """The normalised package name (PEP 503), used to compare names."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _metadata(text: str, where: str) -> tuple[str, str]:
    headers = HeaderParser().parsestr(text)
    name, version = headers.get("Name"), headers.get("Version")
    if not name or not version:
        raise BuildError(f"{where} has no Name or no Version in its METADATA.")
    return name.strip(), version.strip()


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def _check_record(zf: zipfile.ZipFile, dist_info: str, files: list[str], where: str) -> None:
    """Every file in the wheel must be in its RECORD with the right sha256, and the reverse."""
    record_name = f"{dist_info}/RECORD"
    try:
        rows = list(csv.reader(io.StringIO(zf.read(record_name).decode("utf-8"))))
    except KeyError:
        raise BuildError(f"{where} has no {record_name}.") from None
    hashes = {row[0]: row[1] for row in rows if row}
    for name in files:
        if name == record_name or name in {f"{dist_info}/{s}" for s in _RECORD_SIGNATURES}:
            continue
        expected = hashes.pop(name, None)
        if expected is None:
            raise BuildError(f"{where}: the file {name} is not in its RECORD.")
        if expected != _record_hash(zf.read(name)):
            raise BuildError(f"{where}: the file {name} does not match its RECORD (changed).")
    hashes.pop(record_name, None)
    if hashes:
        raise BuildError(f"{where}: its RECORD names files it does not hold: {sorted(hashes)}.")


def _module_name(entry: str, *, is_dir: bool) -> str | None:
    """The name Python imports ``entry`` as, or ``None`` when it is not importable.

    ``entry`` is one name directly in a folder on the import path. A folder is a
    package (with or without ``__init__.py``); a file is a module when it has a
    module ending. ``foo.cpython-312-x86_64-linux-gnu.so`` is the module ``foo``.
    """
    if not is_dir and not entry.endswith(_MODULE_SUFFIXES):
        return None
    name = entry if is_dir else entry.partition(".")[0]
    return name if _FOLDER_RE.match(name) else None


def _is_startup_hook(path: PurePosixPath) -> bool:
    """Whether Python would run ``path`` (relative to site-packages) at start."""
    if path.suffix == ".pth":
        return True
    return _module_name(path.parts[0], is_dir=len(path.parts) > 1) in STARTUP_HOOK_MODULES


def _cache_above_own_folder(parts: tuple[str, ...], owned: int) -> bool:
    """Whether a ``__pycache__`` folder in ``parts`` sits above the wheel's own folder.

    Python loads ``X/__pycache__/name.cpython-NNN.pyc`` instead of ``X/name.py``
    when the compiled file says its source is unchanged. A cache next to the
    base image's modules can so replace their code. ``owned`` is the depth of
    the folder the wheel owns: 1 for a top-level package, 3 for a connector
    folder. A cache inside that folder only replaces the wheel's own code.
    """
    return PYCACHE in parts[:owned]


def _install_path(name: str, data_dir: str, where: str) -> PurePosixPath:
    """The path a wheel entry lands at, relative to site-packages."""
    path = PurePosixPath(name)
    if path.parts[0] == data_dir:
        if len(path.parts) < 3 or path.parts[1] not in ("purelib", "platlib"):
            raise BuildError(
                f"{where}: the file {name} would be written outside the package folder "
                "(scripts, data or headers). Overlay wheels may not do that."
            )
        path = PurePosixPath(*path.parts[2:])
    if _is_startup_hook(path):
        raise BuildError(
            f"{where}: the file {name} is a start-up hook: Python would run it before MEHO "
            "starts. Overlay wheels may not add one."
        )
    owned = 3 if path.parts[0] == CONNECTORS_PREFIX[0] else 1
    if _cache_above_own_folder(path.parts, owned):
        raise BuildError(
            f"{where}: the file {name} is in a compiled-file cache (__pycache__) next to "
            "modules of the base image. Python could load it instead of their source. "
            "Overlay wheels may only have a cache inside their own folder."
        )
    return path


def _connector_folder(path: PurePosixPath, name: str, where: str) -> str:
    parts = path.parts
    if len(parts) < 4 or parts[:2] != CONNECTORS_PREFIX:
        raise BuildError(
            f"{where}: the file {name} is outside its own connector folder "
            "(meho_backplane/connectors/<folder>/). A connector wheel may not write there."
        )
    if not _FOLDER_RE.match(parts[2]):
        raise BuildError(f"{where}: the connector folder name {parts[2]!r} is not valid.")
    return parts[2]


def read_wheel(path: Path, *, connector: bool) -> Wheel:
    """Read and check one wheel file. Nothing is installed or run."""
    where = f"wheel {path.name}"
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildError(f"{where} cannot be read as a wheel: {exc}") from exc
    with zf:
        files = [info.filename for info in zf.infolist() if not info.is_dir()]
        tops = {PurePosixPath(n).parts[0] for n in files}
        dist_infos = {top for top in tops if top.endswith(".dist-info")}
        if len(dist_infos) != 1:
            raise BuildError(f"{where} must have exactly one .dist-info folder.")
        dist_info = dist_infos.pop()
        name, version = _metadata(zf.read(f"{dist_info}/METADATA").decode("utf-8"), where)
        _check_record(zf, dist_info, files, where)
        data_dir = dist_info.removesuffix(".dist-info") + ".data"
        folders: set[str] = set()
        for entry in files:
            if PurePosixPath(entry).parts[0] == dist_info:
                continue
            if connector and PurePosixPath(entry).parts[0] == data_dir:
                raise BuildError(f"{where}: the file {entry} is outside its own connector folder.")
            target = _install_path(entry, data_dir, where)
            if connector:
                folders.add(_connector_folder(target, entry, where))
            elif target.parts[0] == CONNECTORS_PREFIX[0]:
                raise BuildError(
                    f"{where}: the file {entry} would be written into MEHO's own files. "
                    "Only a connector wheel may add a connector folder."
                )
    if connector and not folders:
        raise BuildError(f"{where} adds no connector folder.")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return Wheel(path, name, version, digest, tuple(sorted(folders)))


def _wheel_files(folder: Path | None) -> list[Path]:
    if folder is None or not folder.is_dir():
        return []
    return sorted(folder.glob("*.whl"))


def check_wheels(connectors_dir: Path, deps_dir: Path | None) -> list[Wheel]:
    """Check every wheel before install; return the connector wheels."""
    connectors = [read_wheel(p, connector=True) for p in _wheel_files(connectors_dir)]
    if not connectors:
        raise BuildError(f"no connector wheel (*.whl) found in {connectors_dir}.")
    for path in _wheel_files(deps_dir):
        read_wheel(path, connector=False)
    owner: dict[str, str] = {}
    for wheel in connectors:
        for folder in wheel.folders:
            if folder in owner:
                raise BuildError(
                    f"the connector folder {folder!r} is in both {owner[folder]} "
                    f"and {wheel.path.name}."
                )
            owner[folder] = wheel.path.name
    return connectors


def site_packages(venv: Path) -> Path:
    """The one site-packages folder of a virtual environment."""
    found = sorted(venv.glob("lib/python*/site-packages"))
    if len(found) != 1:
        raise BuildError(f"expected one site-packages folder in {venv}, found {len(found)}.")
    return found[0]


def installed_versions(site: Path) -> dict[str, list[str]]:
    """Map each installed package (normalised name) to [name, version]."""
    versions: dict[str, list[str]] = {}
    for dist_info in sorted(site.glob("*.dist-info")):
        meta = dist_info / "METADATA"
        if meta.is_file():
            name, version = _metadata(meta.read_text(encoding="utf-8"), str(dist_info))
            versions[canonical_name(name)] = [name, version]
    return versions


def file_state(venv: Path) -> dict[str, list[int]]:
    """Every file and link in the environment, with size, change time and inode."""
    state: dict[str, list[int]] = {}
    for current, dirs, files in os.walk(venv):
        here = Path(current)
        for entry in [*files, *(d for d in dirs if (here / d).is_symlink())]:
            path = here / entry
            st = path.lstat()
            state[path.relative_to(venv).as_posix()] = [st.st_size, st.st_mtime_ns, st.st_ino]
    return state


def snapshot(venv: Path) -> dict[str, object]:
    """The state of the base environment before install."""
    return {
        "site_packages": site_packages(venv).relative_to(venv).as_posix(),
        "versions": installed_versions(site_packages(venv)),
        "files": file_state(venv),
    }


def _named(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_NAMED])
    return shown + (f" and {len(items) - _MAX_NAMED} more" if len(items) > _MAX_NAMED else "")


def check_versions(before: dict[str, list[str]], after: dict[str, list[str]]) -> None:
    """No library of the base image may change version or go away."""
    changed = [
        f"{name} {version} -> {after[key][1]}" if key in after else f"{name} {version} -> removed"
        for key, (name, version) in sorted(before.items())
        if after.get(key, [None, None])[1] != version
    ]
    if changed:
        raise BuildError(
            "the connector wheels would change a library of the base image. Every library must "
            f"keep the version of the base image's lock file. Changed: {_named(changed)}."
        )


def _top_modules(entries: list[tuple[str, ...]]) -> set[str]:
    """The names Python can import from a folder, given the paths of its files."""
    return {
        name
        for parts in entries
        if parts and (name := _module_name(parts[0], is_dir=len(parts) > 1))
    }


@dataclass(frozen=True)
class BaseLayout:
    """What the base image has directly in site-packages and in ``connectors/``."""

    tops: set[str]
    modules: set[str]
    connector_modules: set[str]

    @classmethod
    def from_files(cls, before: dict[str, list[int]], site: PurePosixPath) -> BaseLayout:
        size = len(site.parts)
        inner = [
            PurePosixPath(p).parts[size:]
            for p in before
            if PurePosixPath(p).parts[:size] == site.parts
        ]
        in_connectors = [p[2:] for p in inner if len(p) > 2 and p[:2] == CONNECTORS_PREFIX]
        return cls(
            tops={parts[0] for parts in inner if parts},
            modules=_top_modules(inner),
            connector_modules=_top_modules(in_connectors),
        )


def _refusal(
    rel: PurePosixPath, site: PurePosixPath, folders: set[str], base: BaseLayout
) -> str | None:
    """Why a new file is refused, or ``None`` when it is allowed."""
    if rel.parts[0] == "bin" and len(rel.parts) == 2:
        return None
    if rel.parts[: len(site.parts)] != site.parts or len(rel.parts) == len(site.parts):
        return "is outside site-packages and bin"
    inner = PurePosixPath(*rel.parts[len(site.parts) :])
    if _is_startup_hook(inner):
        return "is a start-up hook"
    owned = 3 if inner.parts[0] == CONNECTORS_PREFIX[0] else 1
    if _cache_above_own_folder(inner.parts, owned):
        return "is a compiled-file cache next to modules of the base image (overlap)"
    if inner.parts[0] == CONNECTORS_PREFIX[0]:
        parts = inner.parts
        if len(parts) >= 4 and parts[:2] == CONNECTORS_PREFIX and parts[2] in folders:
            return None
        return "is in MEHO's own files, outside a listed connector folder"
    if inner.parts[0] in base.tops:
        return "is a new file in a folder of the base image (overlap)"
    if _module_name(inner.parts[0], is_dir=len(inner.parts) > 1) in base.modules:
        return "takes the place of a module of the base image (overlap)"
    return None


def check_files(
    before: dict[str, list[int]],
    after: dict[str, list[int]],
    site: PurePosixPath,
    folders: set[str],
) -> list[str]:
    """Base files must stay as they were; new files must sit in new folders, no new module
    or package may have the name of one the base image has, and no new compiled-file cache
    may sit next to base modules. Returns the new files."""
    changed = sorted(p for p, st in before.items() if after.get(p) != st)
    if changed:
        raise BuildError(
            "the install changed or removed files of the base image (overlap with MEHO or with "
            f"another package): {_named(changed)}."
        )
    base = BaseLayout.from_files(before, site)
    if folders & base.connector_modules:
        raise BuildError(
            f"the connector folders {sorted(folders & base.connector_modules)} already exist "
            "in the base image as a folder or a module file under connectors/."
        )
    new = sorted(set(after) - set(before))
    refused = [
        f"{path} ({reason})"
        for path in new
        if (reason := _refusal(PurePosixPath(path), site, folders, base))
    ]
    if refused:
        raise BuildError(f"the install added files that are not allowed: {_named(refused)}.")
    return new


def finish(
    venv: Path, before_file: Path, connectors_dir: Path, out_dir: Path
) -> list[dict[str, object]]:
    """Check the environment after install, copy the new files, and write the allowlist."""
    before = json.loads(before_file.read_text(encoding="utf-8"))
    wheels = [read_wheel(p, connector=True) for p in _wheel_files(connectors_dir)]
    site = PurePosixPath(before["site_packages"])
    after_versions = installed_versions(venv / site)
    check_versions(before["versions"], after_versions)
    folders = {f for w in wheels for f in w.folders}
    new = check_files(before["files"], file_state(venv), site, folders)
    entries: list[dict[str, object]] = []
    for wheel in wheels:
        installed = after_versions.get(canonical_name(wheel.name))
        if installed is None or installed[1] != wheel.version:
            raise BuildError(
                f"the connector package {wheel.name} {wheel.version} was not installed."
            )
        for folder in wheel.folders:
            if not venv.joinpath(site, *CONNECTORS_PREFIX, folder).is_dir():
                raise BuildError(
                    f"the connector folder {folder!r} of {wheel.name} was not installed."
                )
        entries.append(
            {
                "name": wheel.name,
                "version": wheel.version,
                "wheel_digest": wheel.digest,
                "folders": list(wheel.folders),
            }
        )
    target_root = out_dir / venv.relative_to(venv.anchor)
    for path in new:
        target = target_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(venv / path, target)
    allowlist = out_dir / ALLOWLIST_PATH.relative_to("/")
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(json.dumps({"packages": entries}, indent=2) + "\n", encoding="utf-8")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cw = sub.add_parser("check-wheels", help="check the wheel files before install")
    cw.add_argument("--connectors", type=Path, required=True)
    cw.add_argument("--deps", type=Path)
    sn = sub.add_parser("snapshot", help="record the base environment before install")
    sn.add_argument("--venv", type=Path, required=True)
    sn.add_argument("--out", type=Path, required=True)
    fi = sub.add_parser("finish", help="check after install, copy new files, write the allowlist")
    fi.add_argument("--venv", type=Path, required=True)
    fi.add_argument("--before", type=Path, required=True)
    fi.add_argument("--connectors", type=Path, required=True)
    fi.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check-wheels":
            for wheel in check_wheels(args.connectors, args.deps):
                print(f"connector wheel ok: {wheel.name} {wheel.version} {wheel.folders}")
        elif args.command == "snapshot":
            args.out.write_text(json.dumps(snapshot(args.venv)), encoding="utf-8")
        else:
            entries = finish(args.venv, args.before, args.connectors, args.out)
            print("allowlist:", json.dumps({"packages": entries}))
    except BuildError as exc:
        print(f"connector overlay build refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
