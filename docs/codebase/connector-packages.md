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

## Building an overlay image

An overlay image is the public MEHO image plus one or more connector
packages. The build lives in `deploy/connector-overlay/`:

- `Dockerfile` — two stages. The first stage checks and installs the
  wheels. The final stage starts again from the public image and copies in
  only the new files and the allowlist file.
- `overlay_check.py` — the checks. It uses only the Python standard library.
- `build.sh` — makes the build context and runs `docker buildx build`.
- `example/` — a small made-up connector package. CI builds an image from
  it (`.github/workflows/connector-overlay.yml`).

### What a connector package looks like

A connector package is a wheel whose files sit only in its own folders
under `meho_backplane/connectors/`, plus its `.dist-info` folder. It must
not ship `meho_backplane/__init__.py` or `meho_backplane/connectors/__init__.py`.
See `deploy/connector-overlay/example/pyproject.toml` for a working setup.
The libraries it needs come as extra wheels.

### What the build checks

The checks run in the first stage, with the system Python in isolated mode
and without the `site` module (`python3 -I -S`). So a `.pth` file in the
environment cannot run code in the checks.

1. **Before install**, for every wheel file:
   - each file in the wheel must be in its `RECORD` with the right sha256,
     and the reverse. A changed wheel is refused.
   - no start-up hook: no `.pth` file, and no module or package named
     `sitecustomize` or `usercustomize` in any form Python can import
     (a `.py` file, a compiled `.pyc` file, an extension module such as
     `.so`, or a folder). Python would run these before MEHO starts.
   - no compiled-file cache (`__pycache__` folder) above the wheel's own
     folder: not at the top of `site-packages`, not in `meho_backplane/`,
     and not directly in `connectors/`. Python can load a compiled file
     from such a cache instead of the source next to it, so a cache there
     could replace code of the public image. A cache inside the wheel's
     own folder is fine.
   - a connector wheel has files only in `meho_backplane/connectors/<folder>/`.
     Any other file, for example in MEHO's core files, is refused.
   - an extra wheel has no files in `meho_backplane/`, and no files for the
     `scripts`, `data` or `headers` parts of a wheel.
   - two connector wheels may not add the same folder.
2. **Snapshot**: the build records every file of `/app/.venv` and the version
   of every installed library. The public image installs exactly the
   versions of its lock file (`uv sync --locked`), so these versions are the
   lock to check against.
3. **Install**: `uv pip install --offline --no-index --no-build` from the two
   local folders. No package index is used, and nothing is built from source.
4. **After install**, against the snapshot:
   - every library keeps its version. Otherwise the build stops and names
     each library, for example `fakelib 1.0 -> 2.0`.
   - no file of the public image changed or went away.
   - every new file is in a new folder: a new top-level package, a listed
     connector folder that the public image does not have, or a new file in
     `bin/`. A new file in a folder of the public image is refused.
   - no new module or package has the name of one the public image already
     has. Python imports a package folder before a module file of the same
     name, so a connector folder `registry/` would take the place of MEHO's
     `connectors/registry.py`, and an extra wheel's folder `foo/` would take
     the place of a base library's `foo.py`. Both are refused. The same
     checks apply to start-up hooks and to compiled-file caches after
     install: a new `__pycache__` folder next to modules of the public
     image is refused, even when a connector wheel names it as its folder.
   - each connector package is installed, with its folders.
5. **Output**: the new files are copied to an output folder, and the
   allowlist file `/app/connector-packages.json` is written there. For each
   connector wheel it lists the package name and version (from the wheel),
   the sha256 of the wheel file, and its folders.

uv, pip and the wheel files are not in the final image.

### Tier 1: your own image

Anyone can build an overlay image for their own install. evoila does not
sign it, and the appliance does not accept it.

1. Put your connector wheels in one folder and the extra wheels they need
   in another. Get them from your private package repository first; the
   build uses no package index.
2. Pick the public image by its digest:

   ```bash
   docker buildx imagetools inspect ghcr.io/evoila/meho:v<x.y.z>
   ```

3. Build:

   ```bash
   deploy/connector-overlay/build.sh \
     --base ghcr.io/evoila/meho@sha256:<digest> \
     --connectors ./wheels/connectors --deps ./wheels/deps \
     --tag registry.example.com/meho-overlay:<x.y.z>-1 \
     --platform linux/amd64 --push
   ```

   Without `--push`, the image is loaded into the local Docker (one
   platform only).
4. Deploy it with the chart: set `image.repository`, `image.tag` and
   `image.digest` to your image.

### Tier 2: an image evoila signs

Only evoila's own connector packages go into an image that evoila signs.
In the appliance, connector code runs inside the server process, with all
of the server's rights. So a person reviews each package version as
carefully as MEHO's own code, and the review is recorded. An approval by a
bot does not count.

1. A person reviews the wheel with the checklist below and adds a review
   record for it (see "The review record").
2. Run the same build in evoila's CI, with `--push --sign --reviews <folder>`.
   Before the build starts, `build.sh` checks that each connector wheel has
   a review record for its exact name, version and wheel digest. Without
   one, it stops with a message that names the package. `--sign` runs
   `cosign sign --yes <image>@<digest>` on the pushed digest. In GitHub
   Actions this is keyless signing with the workflow's identity, the same
   way the public image is signed (`image.yml`). The job needs
   `id-token: write`.
3. Check the signature before deploying:

   ```bash
   cosign verify <image>@<digest> \
     --certificate-identity-regexp '^https://github.com/<org>/<repo>/' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   ```

4. Deploy by digest, as for tier 1.

#### The review checklist

The reviewer checks each point and confirms it in the record. The key in
brackets is the name of the point in the record.

- Every operation passes the operation contract check
  (`backend/tests/test_operation_contracts.py`). (`operation_contracts`)
- The risk levels and approval flags fit what each operation does. A write
  is not marked as a safe read. (`risk_levels`)
- Secrets are removed from results, from errors and from test captures.
  (`secrets_removed`)
- The code calls only the target's own address, and no other host.
  (`target_address_only`)
- The licences of the package and of all its dependencies fit.
  (`licences`)
- The package has no start-up hooks: no `.pth` file, no `sitecustomize`
  or `usercustomize`, and no code that runs at import beyond registering
  its operations. (`no_startup_hooks`)

#### The review record

A review record is one JSON file for one package version. It lives in the
release repository, in the folder that CI passes to `--reviews`. The
reviewer adds it with a signed-off commit (`git commit -s`), so the
history of the release repository also names the reviewer.

```json
{
  "name": "example-connectors",
  "version": "1.2.0",
  "wheel_digest": "sha256:<64 hex>",
  "reviewer": "Jane Example",
  "date": "2026-10-30",
  "checklist": {
    "operation_contracts": true,
    "risk_levels": true,
    "secrets_removed": true,
    "target_address_only": true,
    "licences": true,
    "no_startup_hooks": true
  }
}
```

- `wheel_digest` is the sha256 of the wheel file, the same value as in the
  allowlist file: `sha256sum <wheel>`, with `sha256:` in front.
- The check (`overlay_check.py check-reviews`) stops when:
  - no record has the wheel's name and version;
  - the record has another wheel digest. A changed wheel needs a new
    review, even with the same version;
  - a field is missing, the date is not `YYYY-MM-DD`, or a checklist
    point is not `true`;
  - the reviewer name ends in `[bot]`.
- Any record file in the folder that cannot be read also stops the check.
- `--reviews` without `--sign` runs the same check as a dry run.

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

## Where operators see the installed packages

The start-up check keeps the packages it accepted. MEHO reads the
allowlist file only once, at start. `installed_connector_packages()` in
`connector_packages.py` returns that list, and these places show it:

- `GET /version` (no login needed): `connector_packages`, next to
  `git_sha`, `build_date` and `chart_version`.
- `GET /api/v1/health` and the MCP tool `meho_status`:
  `connector_packages`.
- `meho version` (when a backplane is known) and `meho status`: a
  `Connector packages:` line, with one line per package.
- The console dashboard: the "Connector packages" row in the Deploy
  card, under the deployed version.

Each entry has the name, version, wheel digest and folders, as in the
allowlist file. For the plain public image the list is empty, and the CLI
and the console say "none".
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
- When the package registers its ops again, it can raise an op's risk
  level or turn on its approval requirement, but never lower them. A
  stricter value saved on the row, for example one an operator set,
  stays. To lower it, the operator edits the op.

"Installed" is per MEHO process. Two servers with different images can
share one database: each one hides only the rows of the packages it does
not have.

Limit: an op of a package whose handler lives outside the package's own
folders (for example in a built-in shared module) is recorded as built
in.

## Key types

- `ConnectorPackageError` — raised when the start must stop. The message
  starts with the error code.
- `ConnectorPackage` — one entry of the allowlist file. `GET /version` and
  `GET /api/v1/health` use the same model for their `connector_packages`
  list.
- `installed_connector_packages()` — the packages the start-up check
  accepted.
- `installed_connector_package_names()`, `connector_package_of()` — the
  installed package names, and the package that owns a module path.
- `installed_owner_clause()`, `package_visible_group_clause()` in
  `operations/_lookup.py` — the filters that hide a removed package's
  rows and groups.
- `CONNECTOR_PACKAGES_FILE`, `BUILTIN_DISTRIBUTION` — the file path and
  the built-in distribution name.

## Secrets in an operation's answer

MEHO hides the value of every secret-named field (`password`, `token`,
`api_key`, `client_secret`, ...) in the answer of every operation, in the
sample rows and in the rows stored behind a result handle. This holds for
a package's operations as for the built-in ones; a package needs no code
of its own for it. A person or a service principal gets the values with
`reveal_secret: true`; an agent is refused. The audit row stores a keyed
fingerprint in place of each hidden value.

A field whose name looks like a secret but whose value is not one, for
example the date `password_expires_at` or the count `secret_id_ttl`, is
hidden too unless the package declares it. The registrar calls
`register_plain_fields(op_id, names)` from `meho_backplane.operations`,
once per operation, next to the operation's registration. A field that is
not declared stays hidden, so a forgotten declaration never shows a secret.
Details: `docs/codebase/redaction.md`, "Secret-named fields in every
answer".

## Known limits

- Code that Python runs before MEHO starts, for example from a `.pth` file
  in an installed package, is not covered by this check. The overlay image
  build refuses such files (see "What the build checks").
- Plain modules (single `.py` files) directly in `connectors/` are not
  imported by the walk, so the check does not look at them.
- The check does not stop public code from importing a connector folder
  that will leave this repository in a package. A separate test does:
  `backend/tests/test_split_ready_imports.py` reads every public module
  and fails when one imports such a folder.
- MEHO must be installed, as a wheel or as an editable install of the same
  checkout. A bare source tree on `PYTHONPATH` that borrows another
  checkout's install has no file list and is outside that checkout, so
  every folder is refused and nothing imports.

## References

- `backend/src/meho_backplane/connector_packages.py`
- `backend/src/meho_backplane/connectors/registry.py`
- `backend/tests/test_connector_packages.py`
- `backend/tests/test_connector_package_owner.py`
- `deploy/connector-overlay/` and `backend/tests/test_connector_overlay_build.py`
- `backend/tests/test_connector_overlay_review.py` — the review record
  check and `build.sh --sign`
- Wheel file format (`RECORD`, `.data` folder):
  https://packaging.python.org/en/latest/specifications/binary-distribution-format/
- `uv pip install`: https://docs.astral.sh/uv/reference/cli/#uv-pip-install
- cosign signing: https://docs.sigstore.dev/cosign/signing/signing_with_containers/
- Python `importlib.metadata`:
  https://docs.python.org/3/library/importlib.metadata.html
- Installed file list (`RECORD`):
  https://packaging.python.org/en/latest/specifications/recording-installed-packages/
- Editable install marker (`direct_url.json`):
  https://packaging.python.org/en/latest/specifications/direct-url-data-structure/
