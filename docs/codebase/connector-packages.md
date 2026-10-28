# Connector packages — the start-up check of connector folders

## Overview

MEHO finds its connectors by walking one folder at start:
`meho_backplane/connectors/`. It imports every subpackage there, and each
subpackage registers its connector and its operations.

A connector package from another repository is a wheel (a built Python
package file) that adds one or more folders to `connectors/`. An overlay
image (a new image built from the public MEHO image) installs such wheels.

Before MEHO imports any connector, it checks every subpackage folder. The
check runs in the `__init__` of the `connectors` package itself. Python
runs that file before any module inside `connectors/`, no matter who
imports it. So the check also runs when a route module in `main.py`
imports one connector directly, which happens long before the server's
start-up steps.

The code of the check lives outside the `connectors/` folder, in
`meho_backplane/connector_packages.py`. Python prefers a folder over a
module file of the same name. If the check were a module inside
`connectors/`, a folder added there with the same name would be imported
in its place, and the planted code would run before any check. A folder
under `connectors/` cannot take the place of a module of `meho_backplane`
itself.

The rules:

- A folder is allowed when all its files were installed by the
  `meho-backplane` distribution. The folder came with MEHO.
- A folder is also allowed when it is named in the allowlist file and all
  its files were installed by the distribution that the allowlist names.
- Any other folder stops the start with `connector_package_not_allowed` and
  the folder name. Nothing is imported, not even the built-in folders.

The plain public image has no allowlist file. There, only the built-in
folders are allowed, and MEHO starts as before.

## The allowlist file

Path: `/app/connector-packages.json`. The overlay image build writes it.

```json
{
  "packages": [
    {
      "name": "example-connectors",
      "version": "1.2.0",
      "wheel_digest": "sha256:<hex of the wheel file>",
      "folders": ["example_widget"]
    }
  ]
}
```

- `name` and `version`: the installed distribution. The version must match
  the installed version exactly.
- `wheel_digest`: the digest of the wheel the build installed. The start
  does not check it; it is shown in the start log line.
- `folders`: the folders the package adds under `connectors/`.

The start stops with `connector_package_list_invalid` when:

- the file is not valid JSON or has missing or unknown fields,
- two entries name the same folder,
- a listed package is not installed, or its version is different.

A listed folder whose files were not all installed by its package stops the
start with `connector_package_not_allowed`.

## How "installed by" is checked

- Every installed distribution has a file list (`RECORD` in its
  `.dist-info` folder). MEHO reads it with `importlib.metadata`.
- For a folder, every file on disk must be in that list. `__pycache__`
  folders are skipped, because Python writes them at run time.
- So a file added later to a built-in folder also stops the start.
- A symbolic link anywhere in a folder, or a folder that is itself a link,
  stops the start. A wheel cannot contain links, and a link can point at
  files that are not in the list and that the walk would not see.

A source checkout set up with `uv sync` is an editable install. Its file
list does not name the source files. For it, every folder inside the
checkout counts as built in. The start log line then says
`source_checkout=True`. The MEHO image installs MEHO from a wheel, not as an
editable install, so the full check applies there.

## Control flow

1. Some code imports `meho_backplane.connectors` or any module under it.
   Python runs `connectors/__init__.py` first.
2. The first thing that file does is call `check_connector_folders()` in
   `meho_backplane/connector_packages.py`. It lists the subpackage folders with
   `pkgutil.iter_modules`, sorted by name, reads the allowlist file,
   checks each listed package, then checks each folder.
3. An unknown folder raises `ConnectorPackageError`. The import of
   `meho_backplane.connectors` fails, so no connector module can load.
   For the server this means `meho_backplane.main` does not even import.
4. On success, one log line, `connector_packages_allowed`, lists the
   allowed packages (name, version, wheel digest, folders).
5. Later, `_eager_import_connectors()` in `connectors/registry.py` imports
   every subpackage so each one registers. It does not check again.

The check is in the package `__init__`, and not in the start-up steps of
the server, because `main.py` imports connector code through its route
modules (for example the Vault and mail connectors) before those steps run.
The server start, the satellite runner start, the OpenAPI schema build and
the topology scope-repair tool all import the package, so all of them are
covered.

## When a package is removed

Each operation row records the package that wrote it, in the column
`endpoint_descriptor.owner_package`. `NULL` means the op is built into
MEHO (ingested ops are `NULL` too).

- At start, the `connectors` package records the allowed packages and
  their folders (`record_installed_connector_packages`).
- When a registrar writes a typed or composite op, MEHO looks at the
  op's `handler_ref`. If it lies in a folder of an installed package
  (`meho_backplane.connectors.<folder>....`), the row gets that package's
  name. Otherwise the row gets `NULL`. Every start writes it again.
- A row whose package is not installed is hidden. `search_operations`
  (also its exact-op mode), `list_operation_groups` and the descriptor
  lookup do not see it. A group that holds only such rows is hidden too.
- A call or a preview of such an op answers
  `connector_package_not_installed`, with the package name in
  `extras.package`.
- Nothing is deleted or changed. When the package is installed again,
  the rows are visible again with the operator's settings on them, for
  example an op the operator turned off stays off.

"Installed" is per MEHO process. Two servers with different images can
share one database: each one hides only the rows of the packages it does
not have.

Limit: an op of a package whose handler lives outside the package's own
folders (for example in a built-in shared module) is recorded as built
in.

## Key types

- `ConnectorPackageError` — raised when the start must stop. The message
  starts with the error code.
- `ConnectorPackage` — one entry of the allowlist file.
- `installed_connector_packages()`, `connector_package_of()` — the
  installed package names, and the package that owns a module path.
- `installed_owner_clause()`, `package_visible_group_clause()` in
  `operations/_lookup.py` — the filters that hide a removed package's
  rows and groups.
- `CONNECTOR_PACKAGES_FILE`, `BUILTIN_DISTRIBUTION` — the file path and
  the built-in distribution name.

## Known limits

- Code that Python runs before MEHO starts, for example from a `.pth` file
  in an installed package, is not covered by this check. The overlay image
  build refuses such files.
- Plain modules (single `.py` files) directly in `connectors/` are not
  imported by the walk, so the check does not look at them.
- MEHO must be installed, as a wheel or as an editable install of the same
  checkout. A bare source tree on `PYTHONPATH` that borrows another
  checkout's install has no file list and is outside that checkout, so
  every folder is refused and nothing imports.

## References

- `backend/src/meho_backplane/connector_packages.py`
- `backend/src/meho_backplane/connectors/registry.py`
- `backend/tests/test_connector_packages.py`
- `backend/tests/test_connector_package_owner.py`
- Python `importlib.metadata`:
  https://docs.python.org/3/library/importlib.metadata.html
- Installed file list (`RECORD`):
  https://packaging.python.org/en/latest/specifications/recording-installed-packages/
- Editable install marker (`direct_url.json`):
  https://packaging.python.org/en/latest/specifications/direct-url-data-structure/
