# Operation catalog file

Every MEHO release has a signed file that lists every operation of every
built-in connector. Automation code can check its calls against this file
with no running MEHO. A second file lists what changed since the previous
release, so a breaking change shows up before an upgrade.

## The files on each release

The GitHub release of each `v*` tag has four files:

| File | What it holds |
|---|---|
| `operation-catalog.json` | Every built-in operation (see below). |
| `operation-catalog.json.cosign.bundle` | Its cosign signature bundle. |
| `operation-catalog-changes.json` | The changes since the previous release tag. |
| `operation-catalog-changes.json.cosign.bundle` | Its cosign signature bundle. |

Check a file before you use it. The signature names the workflow file
`operation-catalog.yml` at the tag:

```bash
cosign verify-blob \
  --certificate-identity "https://github.com/evoila/meho/.github/workflows/operation-catalog.yml@refs/tags/<tag>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --bundle operation-catalog.json.cosign.bundle operation-catalog.json
```

## `operation-catalog.json`

```json
{
  "format_version": 1,
  "release": "<tag>",
  "operations": [
    {
      "connector_id": "k8s-1.x",
      "op_id": "k8s.pod.list",
      "product": "k8s",
      "supported_versions": null,
      "kind": "typed",
      "summary": "...",
      "safety_level": "safe",
      "requires_approval": false,
      "parameter_schema": {"type": "object", "properties": {"...": {}}},
      "result_shape": {"type": "object", "fields": ["..."]}
    }
  ]
}
```

- `connector_id` and `op_id` are the two values `call_operation` takes.
- `supported_versions` is the range of product versions (PEP 440, for
  example `>=8.0,<9.0`) that the connector supports. `null` means any
  version. A multi-step operation (`kind: composite`) uses the connector's
  range for multi-step operations when it has one.
- `safety_level` is the risk level: `safe`, `caution`, `dangerous` or
  `destructive`. `requires_approval` says whether a call waits until a
  person approves it. The file shows the values MEHO ships with. An admin
  of a site can change them for that site's MEHO.
- `parameter_schema` is a JSON Schema 2020-12 document. It is the same
  schema MEHO checks each call against (`validate_params` in
  `backend/src/meho_backplane/operations/_validate.py`). MEHO checks it with
  format checking off, so `format` (for example `uuid`) is a hint only. Use
  a 2020-12 validator with the same setting to get the same answer.
- `result_shape` is a short form of the result: its JSON type and the names
  of its top-level fields (`item_fields` for a list). `null` when the
  operation declares no result schema. A large list result comes back as a
  link to the stored result, not as the list.
- The operations are sorted by `connector_id`, then `op_id`.
- `format_version` goes up when a field changes its meaning or is removed.
  A new field does not change it.

## `operation-catalog-changes.json`

```json
{
  "format_version": 1,
  "release": "<tag>",
  "previous_release": "<previous tag>",
  "added": [{"connector_id": "...", "op_id": "..."}],
  "removed": [{"connector_id": "...", "op_id": "..."}],
  "changed": [
    {
      "connector_id": "...",
      "op_id": "...",
      "changes": [
        {"field": "safety_level", "before": "safe", "after": "caution"},
        {"field": "requires_approval", "before": false, "after": true},
        {"field": "parameter_schema"}
      ]
    }
  ]
}
```

- The previous release is the `v*` tag before this one in the git history.
- A changed parameter schema shows only the field name. Compare the two
  catalog files to see the change.
- A change to the summary or the result shape alone is not listed.
- When the previous release has no catalog (the releases before this file
  existed), `previous_release` is `null` and every operation is `added`.

## How the files are made

1. `cli-release.yml` calls `.github/workflows/operation-catalog.yml` after
   its `release` job.
2. The `build` job runs `backend/scripts/operation_catalog.py export`. The
   script makes a new SQLite database with the current schema and runs
   every connector's registrar into it, the same code a MEHO server runs at
   start. Then it reads the built-in rows back: the `typed` and `composite`
   rows of `endpoint_descriptor` with no tenant and no connector package.
   It needs no running MEHO, no network and no embedding model.
3. The same job downloads the catalog of the previous release tag, checks
   its signature, and runs `operation_catalog.py diff`.
4. The `publish` job signs both files with cosign keyless signing (the same
   way as the CLI release files) and attaches them to the release. It runs
   no repository code.

To write the files on your machine, run from `backend/`:

```bash
uv run python -m scripts.operation_catalog export --release v0.0.0-local --out operation-catalog.json
uv run python -m scripts.operation_catalog diff --new operation-catalog.json --out operation-catalog-changes.json
```

## What is not in the file

- Operations ingested from a site's own API files. They exist only on that
  site's MEHO.
- Operations of connector packages installed next to MEHO.
- Policy: who may call an operation at a site depends on that site's roles
  and grants.

## Tests

`backend/tests/test_operation_catalog_file.py`:

- For sample params of each operation (its examples, a wrong type in each
  field, no params, an unknown param), a 2020-12 validator with the schema
  from the file and `validate_params` give the same answer.
- A changed risk level and approval flag show under `changed`, with the
  values before and after.
- Added, removed and schema-changed operations are listed.
- The two commands the workflow runs write both files.
