# Connector packages — the start-up check of connector folders

## Overview

MEHO finds its connectors by walking one folder at start:
`meho_backplane/connectors/`. It imports every subpackage there, and each
subpackage registers its connector and its operations.

A connector package from another repository is a wheel (a built Python
package file) that adds one or more folders to `connectors/`. An overlay
image (a new image built from the public MEHO image) installs such wheels.

Before MEHO imports anything, it now checks every subpackage folder:

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

A source checkout set up with `uv sync` is an editable install. Its file
list does not name the source files. For it, every folder inside the
checkout counts as built in. The start log line then says
`source_checkout=True`. The MEHO image installs MEHO from a wheel, not as an
editable install, so the full check applies there.

## Control flow

1. `_eager_import_connectors()` in `connectors/registry.py` lists the
   subpackages with `pkgutil.iter_modules`, sorted by name.
2. `check_connector_folders()` in `connectors/packages.py` reads the
   allowlist file, checks each listed package, then checks each folder.
3. Only when every folder is allowed are the subpackages imported.
4. One log line, `connector_packages_allowed`, lists the allowed packages
   (name, version, wheel digest, folders).

The same walk runs in the server start, the satellite runner start, the
OpenAPI schema build and the topology scope-repair tool.

## Key types

- `ConnectorPackageError` — raised when the start must stop. The message
  starts with the error code.
- `ConnectorPackage` — one entry of the allowlist file.
- `CONNECTOR_PACKAGES_FILE`, `BUILTIN_DISTRIBUTION` — the file path and
  the built-in distribution name.

## Known limits

- Code that Python runs before MEHO starts, for example from a `.pth` file
  in an installed package, is not covered by this check. The overlay image
  build refuses such files.
- Plain modules (single `.py` files) directly in `connectors/` are not
  imported by the walk, so the check does not look at them.

## References

- `backend/src/meho_backplane/connectors/packages.py`
- `backend/src/meho_backplane/connectors/registry.py`
- `backend/tests/test_connector_packages.py`
- Python `importlib.metadata`:
  https://docs.python.org/3/library/importlib.metadata.html
- Installed file list (`RECORD`):
  https://packaging.python.org/en/latest/specifications/recording-installed-packages/
- Editable install marker (`direct_url.json`):
  https://packaging.python.org/en/latest/specifications/direct-url-data-structure/
