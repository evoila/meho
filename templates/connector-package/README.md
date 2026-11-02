# A connector package: the template

This folder is a working start for a MEHO connector package. A connector
package is a wheel (a built Python package file) that adds one connector to
MEHO. MEHO runs its operations with the same policy, approvals, audit,
secret removal and result handles as its own connectors.

The folder holds a made-up connector for a made-up "widget" API. It
imports MEHO only through the connector kit, the module
`meho_connector_kit`. Every other MEHO module is private and can change in
any MEHO release.

## Start

1. Copy this folder into a repository of your own.
2. Rename the made-up names:

   | Made-up name | Where |
   |---|---|
   | `example-connectors` (the wheel's name) | `pyproject.toml` |
   | `example_widget` (the folder) | the folder name, `pyproject.toml`, `PACKAGE` in `tests/test_kit_imports.py`, the imports in `tests/test_example_widget.py` |
   | `example`, `example-widget`, `1.x` (product, implementation id, version) | `connector.py`. The product must be the first part of the implementation id. The connector id is `<implementation id>-<version>`, here `example-widget-1.x`. |
   | `ExampleWidgetConnector` | `connector.py`, `operations.py`, `__init__.py`, the tests |
   | `example.widget.list`, `example.widget.rename` (operation ids) | `operations.py` |

3. Change the calls in `connector.py` to the API of your system, and the
   operations and their texts in `operations.py`.

## The files

```text
pyproject.toml             the wheel: name, version, kit range, the one folder it holds
meho_backplane/connectors/example_widget/
  __init__.py              registers the connector, its operations and its preview
  connector.py             ExampleWidgetConnector, an HttpConnector
  operations.py            the two operations and their texts
tests/
  test_example_widget.py   both operations through MEHO, against a fake widget API
  test_kit_imports.py      the package imports MEHO only through the kit
```

The two operations:

- `example.widget.list` is a safe read. It reads every page of the list.
  MEHO turns a long list into a result handle (a link to the stored
  result) by itself.
- `example.widget.rename` is a change. MEHO waits until a person approves
  it. While it waits, the preview shows the person the widget's current
  name and the new name. The widget is found by its id, so the approved
  call changes the widget the person saw.

The connector reads its token with `load_vault_secret_data`, as the
caller. It never puts the token into an answer or an error.

## The kit range

`pyproject.toml` says which kit versions the package fits:
`dependencies = ["meho-connector-kit>=1.0,<2"]`. The kit has its own
version. It is not the MEHO release number.

- The first number goes up when a change can break a package.
- The second number goes up when names are only added.

The kit names and the kit version of a MEHO release are in
`backend/connector-kit/` of that release. The kit is not on a public
package index: it ships only with MEHO.

## Tier 1 and tier 2

- **Tier 1, open source.** You build connectors against the kit and add
  them to your own build of MEHO, at your own risk. Your package imports
  MEHO only through the kit, like this template.
- **Tier 2, the appliance.** Only evoila's own connectors ship in the
  appliance. A person reviews each wheel, and evoila signs the image.

## Test against the MEHO release you build for

The tests run inside a MEHO checkout, at the release tag you build for:

```bash
git clone https://github.com/evoila/meho.git
cd meho
git checkout <release-tag>
cd backend
uv sync --locked --all-groups
cp -r <your-repo>/meho_backplane/connectors/example_widget src/meho_backplane/connectors/
cp -r <your-repo>/tests tests/example_widget
uv run pytest tests/example_widget tests/test_operation_contracts.py
```

- `tests/test_example_widget.py` runs both operations through MEHO. It may
  use MEHO's own test helpers: they carry no promise, so check them against
  each new release.
- `tests/test_kit_imports.py` fails when a file of the package imports a
  MEHO module other than the kit or the package's own folder.
- `tests/test_operation_contracts.py` is MEHO's check of the operation
  texts and schemas. Run it with the package's folder in the checkout.

Then build the wheel and run the wheel checks of the overlay build:

```bash
uv build --wheel --out-dir dist <your-repo>
python3 -I -S ../deploy/connector-overlay/overlay_check.py check-wheels --connectors dist
```

To build an image with your package, use
`deploy/connector-overlay/build.sh`. See
[`docs/codebase/connector-packages.md`](../../docs/codebase/connector-packages.md),
"Building an overlay image".

In this repository, the CI job "Connector package template" runs these
steps against the code of each pull request. So a change to the kit that
breaks this template fails in the same pull request.

## More to read

- [`docs/codebase/connector-packages.md`](../../docs/codebase/connector-packages.md):
  how MEHO checks connector folders, the connector kit, the overlay build
  and the review.
- [`docs/decisions/2026-11-01-connector-kit.md`](../../docs/decisions/2026-11-01-connector-kit.md):
  why the kit is one module with its own version.
