# Migration assessment package (`connectors/migration`)

## Overview

This package holds the shapes and the operations of the migration
assessment work: MEHO reads the estate a team runs today and turns it
into an Azure migration assessment that people can check.

The first part, in this package today:

- **One inventory shape** for every source platform (`contracts.py`).
- **One assessment result shape** (`contracts.py`).
- **A loader** that reads stored inventories back by handle id and
  merges them (`inventory_load.py`).
- **The Azure Migrate CSV mapping** (`azure_migrate_csv.py`) and the
  read `migration.inventory.csv` (`ops.py`).

Like `connectors/audit/`, the package has no connector class. Its
operations are registered under the natural key
`(product="migration", version="1.x", impl_id="migration-assess")`, so the
connector id is `migration-assess-1.x`. The handlers are module-level
functions; the dispatcher calls them with `target=None`. The package
`__init__` queues `register_migration_operations` on the lifespan
registrar list (`register_typed_op_registrar`).

## Key types

### Inventory envelope (`InventoryEnvelope`)

One inventory from one source:

| Field | Meaning |
|---|---|
| `schema_version` | `"1"`. Readers refuse any other value. |
| `source` | `connector_id`, `target` name, `collected_at`. |
| `records` | Records of three kinds, told apart by `kind`. |
| `omissions` | Parts of the estate that were not collected, with a reason. |
| `counts` | Records per kind, plus `source_totals`: the source's own total per kind, for checking. |

The model checks that `counts` match the records and that no
`source_id` repeats.

Record kinds:

- `ServerRecord` (`kind="server"`): `source_id`, `identity_hints`
  (BIOS UUID, MAC addresses, host name, all lower case), `name` (or
  `null` with a reason),
  `server_type`, `hypervisor`, `cores`, `memory_configured_mb`,
  `memory_assigned_mb`, `disks` (size, used space, thin, shared, raw
  mapping, independent, and read/write speeds), `snapshot_count`, `os`
  (name, version, family, architecture), `boot_type` (`bios` or `uefi`),
  `secure_boot`, `power_state`, `nics`, `utilization` (CPU, memory and
  network as average, p95 and maximum, with the window, the number of
  samples and the source) or `null`, `collected_at`, `missing`.
- `ApplicationRecord` (`kind="application"`): name, version, vendor,
  category, and `runs_on` (server `source_id` values).
- `KubernetesWorkloadRecord` (`kind="kubernetes_workload"`): cluster,
  namespace, `workload_kind`, name, replicas, CPU and memory requests and
  limits, images, volumes and the nodes its pods run on.

Rules every record follows:

- **Units are in the field name**: `_mb`, `_gb`, `_mb_per_s`,
  `_millicores`, `_percent`.
- **Unknown is `null`, never `0`.** Every field that is `null` must be
  covered in `missing` with a reason (`not_reported_by_source`,
  `no_permission`, `powered_off`, `not_measured`, `collection_failed`,
  `not_supported`). This includes nested fields and list items: the key
  is the dotted path, for example `os.version`, `disks.0.used_gb` or
  `utilization.cpu_percent.average`. A parent path (`disks.0`, `disks`)
  covers every unknown field below it. The model refuses a record that
  breaks this rule.
- Unknown fields are refused (`extra="forbid"`), so a typo in a producer
  fails at once.

### Assessment result (`AssessmentResult`)

`calculation_version`, `inputs` (the inventory handles with their
connector, target and time, other targets read, and when it ran),
`assumptions` (region, currency, price date, hours per month, license,
reservation, comfort factor, and free `other` values), `items`,
`unknowns`, `omissions`.

Every number is a `Figure`: `value`, `unit`, `basis` (`measured` or
`allocated`; never "right-sized" without measurements), `inputs`, the
names of the `assumptions` it used, `calculation_version`, and
`unknown_reason` when `value` is `null`. The model refuses a figure that
cites an assumption that is not set.

`inventory_json_schema()` and `assessment_result_json_schema()` export the
two shapes as JSON Schema for the operations' response schemas.

## Control flow

### Stored inventories

An inventory read returns one envelope. The envelope has two lists
(`records` and `omissions`), so when it is over the inline size limit the
JSONFlux reducer stores it **whole** as a document record and the caller
gets a handle id (see `jsonflux.md`). `load_inventories(operator,
handle_ids)`:

1. Reads each handle from `get_result_handle_store()` with the caller's
   tenant id. The store also checks the caller, so another tenant's or
   another person's handle is a miss.
2. Fails with one clear message when a handle is unknown or expired (a
   stored result lasts one hour by default), when it holds rows instead of
   one inventory, when `schema_version` is not `"1"`, or when the envelope
   is not valid. Validation messages leave out the input values.
3. Decodes and checks each record in a worker thread
   (`asyncio.to_thread`), because a stored result can be several MB.
4. Merges the envelopes in handle order (`merge_inventories`). A handle
   given twice is read once. A repeated `source_id`, or a server whose BIOS
   UUID was already seen, stays in `records` and is listed in
   `duplicates`; the caller decides what that means.

### `migration.inventory.csv`

Group `inventory`, `safe`, `requires_approval: false`. It is a typed
operation, not a multi-step one: it runs no other operation, it only reads
stored results. In: `inventory_handles` (1 to 20). Out: `csv_lines` (the
file, one item per line without the line end, header first), `row_count`,
`columns`, `left_out`, `renamed`, `skipped_records`, `notes`, `sources`.
Join `csv_lines` with CRLF to get the file.

The file is a list of lines on purpose. A large result comes back as a
handle, and `result_query` returns an array in pages (`offset` and
`limit`, up to 500 items a page) but a string only whole, under one output
limit (256 KiB by default). A file of 10,000 servers is over that limit as
one string. Read the lines in pages with `result_query`, the path
`/csv_lines` and `offset` / `limit`; each page holds rows of
`{"value": <line>}`.

The mapping (`inventory_to_csv`):

- Column names are copied from Microsoft's import template
  (`AzureMigrateimporttemplate.csv`, linked from
  <https://learn.microsoft.com/en-us/azure/migrate/tutorial-discover-import>).
  A `*` marks a required column, as in the template. The file has as many
  disk groups (five columns each) as its largest server needs: at least the
  template's two, at most twenty.
- Only servers go into the file. Other kinds are counted in
  `skipped_records`.
- Left out, with the reason: a server without name, cores, memory or OS
  name (`missing_required_field`, with the reasons from `missing`; a name
  or OS name that is empty after cleaning counts as missing); a
  server already in the file through another source, found by BIOS UUID
  (`same_machine_as`); servers past 20,000 (`over_file_limit`). Required
  values are checked first, so when one record of a machine lacks a value,
  a complete record of the same machine from another source goes in.
- Names must be unique, because Azure Migrate updates a server imported
  again with the same name. A name that another server has, or that an
  earlier server already got (`sample` renamed to `sample (x:1)` takes the
  name of a server called `sample (x:1)`), gets its `source_id` added, then
  a counter while it is still taken. Case does not count. `renamed` says so.
- Values are only rounded: the inventory units already match the template.
  Utilization columns take the p95 value. Unknown values stay empty.
- Text from source systems: line breaks become spaces, and a cell that
  starts with `=`, `+`, `-` or `@` gets a leading `'`, so a spreadsheet
  does not read it as a formula. A changed name is listed in `renamed`
  with `cleaned_text`.
- Line ends are CRLF (RFC 4180); `LINE_END` in `azure_migrate_csv.py`.
  The file has no byte order mark.

## Dependencies

- `pydantic` v2 models (`model_validator`, discriminated union on `kind`).
- `connectors/result_handle_store.py` (`fetch_record`, `decode_document`).
- `operations/typed_register.py` (`register_typed_operation`,
  `register_typed_op_registrar`).
- Python's `csv` module.

## Tests

`backend/tests/test_migration_contracts.py` pins the shapes with made-up
fixtures: a Windows VM with three disks, a Linux VM with no utilization,
the same Linux machine read through its guest OS (same BIOS UUID), a
Kubernetes workload and an application. Inventories are stored through the
real JSONFlux reducer and read back through the loader. The golden file
`backend/tests/data/migration_azure_migrate_golden.csv` pins the CSV
output; the test also checks its header against Microsoft's template
header, column for column.

## Known issues

- Only handles are accepted. An inventory small enough to come back inline
  has no handle yet; the inventory reads of the later work decide how they
  store their result.
- Duplicate servers are matched by exact BIOS UUID only. Some guest
  systems report the BIOS UUID with the first three groups byte-swapped;
  the source adapters must normalise it before it reaches `identity_hints`.

## References

- Microsoft: Build a business case or assess servers using an imported
  CSV file,
  <https://learn.microsoft.com/en-us/azure/migrate/tutorial-discover-import>.
- `docs/codebase/jsonflux.md` — stored results and handles.
- `docs/codebase/audit_query.md` — the same package pattern
  (`audit-integrity-1.x`).
