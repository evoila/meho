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

The source adapters live with their connectors. The first one is the VMware
read `vmware.migration.inventory` (see "VMware source adapter" below).

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
| `counts` | Records per kind, plus `source_totals`: the source's own total per kind, and `reconciliation`: the count check per kind. |
| `duplicates` | Records that look like a machine already in the list (same BIOS UUID or instance UUID). Both records stay; later steps count the machine once. |

The model checks that `counts` match the records and that no
`source_id` repeats. It also checks that every `duplicates` entry names
records of the inventory, and that each `reconciliation` entry adds up:

- `source_total`: the total the source reports (equal to
  `source_totals[kind]`).
- `records`: the records of that kind.
- `omitted`: the omissions of that kind. An omission with `kind` and
  `source_id` set stands for one item of the source that has no record
  (one left-out VM). An omission without `kind` is a part of the work that
  failed as a whole (for example "utilization from VCF Operations").
- `difference` = `source_total` - `records` - `omitted`, and `matches` is
  true exactly when the difference is 0. A difference is reported, not
  hidden.

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
  samples and the source) or `null`, `collected_at`, `missing`, and
  `source_attributes` (more facts with fixed keys per source; VMware:
  `vm_id`, `instance_uuid`, `esxi_host`, `cluster`).
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
   `duplicates`; the caller decides what that means. A repeated record's
   BIOS UUID still counts, so a machine whose first record had no BIOS
   UUID is still matched to a later record from another source.

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

### VMware source adapter: `vmware.migration.inventory`

A read-only multi-step operation of the vCenter connector
(`connectors/vmware_rest/composites/_migration_inventory*.py`), registered
on both vCenter catalogs (`vmware-rest-8.0` and `vmware-rest-9.0`), group
`migration`, `safe`, no approval. It returns one inventory envelope; a
large one comes back as a handle for `migration.inventory.csv`.

Parameters: `vcf_operations_target` (optional), `utilization_days` (1 to
365, default 30), and `cluster` or `folder` (an id) to narrow the scope.

Steps:

1. `RetrieveServiceContent`: the root folder, the performance manager and
   the vCenter instance UUID.
2. One paged `RetrievePropertiesEx` from the scope down to every VM (through
   the hosts, or through a VM folder and its vApps): name and template flag.
   The number found is vCenter's own count, `source_totals.server`.
3. Templates are left out: one omission each, reason `not_supported`.
4. The other VMs in batches of 20: one `RetrievePropertiesEx` for the record
   properties (`DETAIL_PROPERTIES` in `_migration_inventory_map.py`). A
   failed batch is read again one VM at a time; a VM that still fails is an
   omission (`collection_failed`), not a failed run.
5. The ESXi host and cluster names, read once for all hosts (best effort).
6. Utilization per batch (below), then the records, the duplicates and the
   count check.

The record, from the VM's properties:

| Field | From |
|---|---|
| `source_id` | `vmware:<vCenter instance UUID>:<VM instance UUID>`; the VM id (`vm-42`) instead when the instance UUID is missing or already used |
| `identity_hints` | `config.uuid` (BIOS UUID), NIC MAC addresses, `guest.hostName` |
| `cores`, `memory_configured_mb` | `config.hardware.numCPU`, `config.hardware.memoryMB` |
| `memory_assigned_mb` | the configured memory while the VM runs (vSphere has no dynamic memory); unknown (`powered_off`) when it is off |
| `disks` | each `VirtualDisk`: size; thin (flat disk flag, sparse disks are thin); shared (`sharingMultiWriter`); raw mapping (RDM backing); independent (`diskMode`). Used space and disk speeds are unknown with a reason |
| `snapshot_count` | the nodes of `snapshot.rootSnapshotList`; 0 when the VM has none |
| `os` | `guest.guestFullName`, else `config.guestFullName`; family from `guest.guestFamily`, else from `config.guestId`; `x64` or `arm64` from the guest id |
| `boot_type`, `secure_boot` | `config.firmware` (`efi` is `uefi`), `config.bootOptions.efiSecureBootEnabled`. An EFI VM without the flag has secure boot off; a flag vCenter could not read is unknown, with the reason |
| `power_state`, `nics` | `runtime.powerState`; NIC devices with their guest IP addresses and network |
| `source_attributes` | `vm_id`, `instance_uuid`, `esxi_host`, `cluster` |

A property vCenter refuses (`NoPermission`) makes its field unknown with the
reason `no_permission`.

Utilization:

- **Without `vcf_operations_target`**: vCenter's performance history, one
  `QueryPerf` per batch for `cpu.usage.average`, `mem.usage.average`,
  `net.received.average` and `net.transmitted.average` (aggregate instance
  only). The counter ids come from `PerformanceManager.perfCounter`. The
  sample interval follows the window: 5 minutes up to a day, 30 minutes up
  to a week, 2 hours up to 30 days, 1 day above. Average, p95 (nearest rank)
  and maximum; vCenter's `-1` (no sample) is skipped. `source` is
  `vcenter-performance`.
- **With `vcf_operations_target`**: per VM, the child operations
  `vrops.resource.query` (by name, then matched on the VM id and vCenter
  UUID, or the instance UUID) and `vrops.resource.stats` on that target. Each
  child has its own policy check and audit row. That read gives the latest
  values only, so the window is the time of those samples and `samples` is
  small; `source` is `vcf-operations-latest`. The first child answer that is
  denied or waits for approval stops the source: the rest of the VMs get
  `no_permission`, and one omission without `kind` says why.
- No samples: `utilization` is `null` with `powered_off` (the VM is off) or
  `not_measured`. A failed read gives `collection_failed`. Never zero.

Duplicates: a record whose BIOS UUID or instance UUID an earlier record
already has (for example a VM registered again from copied files) is listed
in `duplicates`. Both stay in `records`, because vCenter counts both. The
CSV export leaves the later one out as `same_machine_as`.

Tests: `backend/tests/test_vmware_migration_inventory.py` runs the operation
against a fake vCenter with made-up VMs (Windows with three disks, Linux
without samples, a copy with the same BIOS UUID, a template, a VM that cannot
be read) and checks the values, the count check, the duplicates and the CSV
export of the stored result.

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
- The VCF Operations source gives only the latest values, not a history
  window. A history read of VCF Operations would need a new operation of
  that connector.
- The VMware adapter reads vCenter targets. A standalone ESXi host is not
  covered.
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
