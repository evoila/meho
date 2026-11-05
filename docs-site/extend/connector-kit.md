# Write a connector package

A connector package adds a connector to MEHO from a repository of your
own. You write the connector against the **connector kit**: one small
Python module with the MEHO names a package may use. MEHO then runs the
package's operations like the ones it ships itself.

This page is for people who write a connector. If you run MEHO and want
to use connectors, start with [Do real work](../guides/index.md).

!!! note "Words used on this page"

    - **Wheel:** a built Python package file (`.whl`).
    - **Connector package:** a wheel that adds one or more connector
      folders under `meho_backplane/connectors/`.
    - **Connector kit:** the module `meho_connector_kit`, from the package
      `meho-connector-kit`. It holds the MEHO names a package may import.
    - **Kit version:** the version of `meho-connector-kit`, for example
      1.0. It is not the MEHO release number.
    - **Range:** the kit versions your package fits, for example
      `>=1.0,<2`.
    - **Overlay image:** a new image built from the public MEHO image,
      with connector packages added.

## 1. What you build

You build a wheel with one folder under `meho_backplane/connectors/`, for
example `meho_backplane/connectors/example_widget/`. The wheel has no
other files. The folder holds:

- the connector class, which talks to the vendor's API;
- the operations: hand-written functions that MEHO runs when an agent or
  a person calls them.

MEHO runs your operations the same way as its own. Each call goes through
MEHO's policy check, waits for a person's approval when it must, is
written to the audit log, has secret values removed from its answer, and
gets a result handle (a link to the stored result) when the answer is a
long list. Your code does none of this.

There are two ways to bring a package into MEHO:

- **Tier 1: your own image.** You build an overlay image for your own
  install. evoila does not sign it.
- **Tier 2: an image evoila signs.** Only for evoila's own packages. A
  person reviews each package version before evoila signs the image.

## 2. Start from the template folder

The template folder is
[`templates/connector-package/`](https://github.com/evoila/meho/tree/main/templates/connector-package)
in the MEHO repository. It holds a small working sample connector, its
tests and its `pyproject.toml`.

1. Copy the folder into a repository of your own.
2. Rename the made-up names: the package name `example-connectors`, the
   folder `example_widget`, the product `example` and the
   implementation id `example-widget`.
3. Replace the made-up widget API with the API of your product.

## 3. The connector kit

Your package imports MEHO only through one module:

```python
import meho_connector_kit as kit
```

The kit version this page describes: **1.2**.

The names below are the promise. Every other MEHO module is private. It
can change in any MEHO release, without notice. A module whose path has a
part that starts with an underscore is private by its name too.

| Group | Name | What a package uses it for |
|---|---|---|
| Register | `register_connector_v2` | Register the connector class under its product, version and implementation id. |
| Register | `register_typed_op_registrar` | Queue the function that registers your operations when MEHO starts. |
| Register | `register_typed_operation` | Register one operation: the shape of its inputs and its answer, its risk level, whether it waits for approval, and its texts for agents. |
| Register | `register_connector_catalog` | Bring the catalog rows of your package: for a product and version, the vendor API file MEHO can load, the ingest profile, and the safety overlay for the operations made from that file. |
| Register | `CALLER_ADDRESS_MARKER` | The schema keyword `x-meho-caller-address`. Put `CALLER_ADDRESS_MARKER: True` on each input that holds an address MEHO connects to. See "An input that holds an address" below. |
| Register | `EmbeddingService` | The type of the `embedding_service` value that MEHO hands to your registering function. Pass it on unchanged. |
| Connector | `Connector` | The base class of every connector. |
| Connector | `HttpConnector` | The base class for an HTTP API. It brings MEHO's checks for TLS (secure, encrypted connections), addresses, redirects and answer size. |
| Connector | `Target` | The target fields a connector may read. |
| Answers | `FingerprintResult` | The answer of `fingerprint`: the detected product and version. |
| Answers | `ProbeResult` | The answer of `probe`: can MEHO reach the target. |
| Answers | `OperationResult` | The answer of `execute`. |
| Answers | `AuthModel` | How a target logs in. |
| Errors | `ConnectorUpstreamError` | The vendor answered with an error status. |
| Errors | `ConnectorAuthError` | The vendor refused the credential. |
| Errors | `CredentialsReadError` | MEHO could not read the target's secret. |
| Errors | `VaultCredentialsReadError` | MEHO could not read the target's secret from Vault. |
| Errors | `VaultClientError` | MEHO could not log in to Vault for the caller. |
| Caller | `Operator` | The person or agent who called. Pass it on. |
| Caller | `synthesise_system_operator` | MEHO's own caller, for checks that run without a person. |
| Caller | `is_system_operator` | Check if the caller is MEHO's own caller. |
| Secrets | `load_vault_secret_data` | Read the target's secret from Vault, as the caller. |
| Secrets | `load_param_secret_data` | Read a secret that an operation input names, after MEHO checks that the caller may name it. |
| Secrets | `strip_credential_value` | Clean a secret value, for example remove a new line at the end. |
| Secrets | `is_acceptable_auth_model` | Check that the target uses the shared service account login. |
| Secrets | `target_cache_key` | A cache key for one target that keeps tenants apart. |
| Approvals | `PreviewContext` | What a preview gets: the target, the caller, the inputs and the connector. |
| Approvals | `PreviewBuilder` | The type of a preview: it shows a person what a change will do, while the change waits for approval. |
| Approvals | `register_preview_builder` | Register a preview for one operation. |
| Approvals | `PermissionPreflight` | The type of a check that runs while a change waits: may the caller make it? |
| Approvals | `register_permission_preflight` | Register such a check for one operation. |
| Audit | `compute_audit_digest_hash` | A hash made with a secret key of MEHO, for a value that lands in an audit or approval row. |

Inside three classes, the kit also promises some parts:

- `HttpConnector`: `__init__`, `auth_headers`, `aclose`,
  `trace_response_body`, `_get_json` and `_post_json`. The last two
  start with an underscore, but your subclass calls them, so they are part
  of the kit.
- `PreviewContext`: `target`, `operator`, `params`, `connector_instance`
  and `connector_id`.
- `Target`: `name`, `host`, `extras`, `auth_model`, `verify_tls` and
  `secret_ref`. Any other field of a target is private.

For every other class, the kit promises only the name.

## 4. Write the connector

The connector is a subclass of `kit.HttpConnector` (for an HTTP API) or
of `kit.Connector`. It sets `product`, `version` and `impl_id`, and has
`fingerprint` and `probe`. You do not write `execute`: MEHO runs the
operations you register, not `execute`.

- **The target.** Read only the six fields of `kit.Target`. Put settings
  that only your connector needs in `extras`.
- **Secrets.** Read the target's secret with `kit.load_vault_secret_data`.
  It reads as the caller, so MEHO's rules for who may read what still
  hold. Never write a secret into an answer, an error text or a log line.
- **Errors.** Raise `kit.ConnectorUpstreamError` when the vendor answers
  with an error, and `kit.ConnectorAuthError` when it refuses the
  credential. A failed read must never look like an empty answer.
- **Caches.** Key a cache you keep across calls, for example a login
  token, with `kit.target_cache_key(target)`.

The steps, with code, are in
[connector-packages.md, "Writing the connector"](https://github.com/evoila/meho/blob/main/docs/codebase/connector-packages.md#writing-the-connector).
More to read:

- secrets:
  [connectors-shared-vault-creds.md](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-shared-vault-creds.md);
- error texts:
  [error-message-shape.md](https://github.com/evoila/meho/blob/main/docs/codebase/error-message-shape.md).

## 5. Add operations

Your folder's `__init__.py` calls `kit.register_typed_op_registrar` with
one function. MEHO runs that function when it starts. The function calls
`kit.register_typed_operation` once for each operation. Do not register
the operations when the folder is imported: the import must stay quick
and must not touch the database.

The inputs of `register_typed_operation`:

- **Where it belongs:** `product`, `version`, `impl_id` and `op_id`, for
  example `example.widget.list`.
- **What runs:** `handler`, an async function or an async method of your
  connector class. It gets the caller, the target and the inputs, and
  returns plain data (a dict).
- **Shapes:** `parameter_schema` and `response_schema`, as JSON Schema.
- **Risk level:** `safety_level` is `safe`, `caution`, `dangerous` or
  `destructive`. Every operation that is not `safe` must set
  `requires_approval=True`: MEHO then waits until a person approves the
  call.
- **Texts for agents:** `summary`, `description`, `group_key`,
  `when_to_use` and `llm_instructions`. An agent picks your operation
  from these texts, so write them with care.
- `embedding_service`: pass on the value MEHO hands to your function.

**An input that holds an address.** Some operations connect to an
address the caller gives: a URL, a host name, or a host and port. The
connection can come from MEHO, or from the target for MEHO (for example a
ping that a firewall sends). Mark each such input in `parameter_schema`
with `kit.CALLER_ADDRESS_MARKER: True`, also when it sits inside a nested
object or a list:

```python
"properties": {
    "url": {"type": "string", kit.CALLER_ADDRESS_MARKER: True, "description": "..."},
}
```

MEHO then runs the operation at risk level `caution` or higher, and it
waits until a person approves each call. This holds whatever
`safety_level` you set, and an operator override cannot turn the approval
off. Set `safety_level="caution"` (or higher) and `requires_approval=True`
yourself too, so your code says what MEHO does. Do not mark an input
that only stores or names an address and never connects to it, for
example a host name written into an inventory record. MEHO's sensors run
only `safe` operations, so a sensor cannot use a marked operation.

**A preview for a change.** Register a preview with
`kit.register_preview_builder`. It shows the person who approves what the
change will do. Every `destructive` operation needs one.

What MEHO does for you, so you do not:

- the policy check and the approval wait
  ([approvals.md](https://github.com/evoila/meho/blob/main/docs/codebase/approvals.md));
- the audit row;
- removing secret values from the answer;
- result handles for long lists
  ([jsonflux.md](https://github.com/evoila/meho/blob/main/docs/codebase/jsonflux.md)).

**Catalog rows.** MEHO can also make operations from a vendor's API file
(an OpenAPI file). The catalog says, per product and version, which file
to load, how to log in (the ingest profile), and which of the operations
made from the file need a higher risk level or an approval (the safety
overlay). Your package can bring such rows. Put a `catalog.yaml` with the
same shape as MEHO's own
[`catalog.yaml`](https://github.com/evoila/meho/blob/main/backend/src/meho_backplane/operations/ingest/catalog.yaml)
in your connector folder, next to the API file and the profile it names,
and call `kit.register_connector_catalog(__name__)` from the folder's
`__init__.py`. MEHO reads the rows when it starts and checks them like its
own. A row for a product and version that MEHO already has stops the
start. The details are in
[connector-packages.md, "Bring catalog rows"](https://github.com/evoila/meho/blob/main/docs/codebase/connector-packages.md#bring-catalog-rows).

## 6. Say which MEHO releases fit

Your package names the kit versions it fits, in its `pyproject.toml`:

```toml
[project]
name = "example-connectors"
version = "0.1.0"
dependencies = ["meho-connector-kit>=1.0,<2"]
```

`>=1.0,<2` means kit version 1.0 or later, but before 2. Use the kit
version of the oldest MEHO release you test against as the lower end.

**The kit version of a MEHO release** is in
`backend/connector-kit/pyproject.toml` at the release tag. You can also
read it from the image:

```bash
docker run --rm --entrypoint python ghcr.io/evoila/meho:v<x.y.z> \
  -c "from importlib.metadata import version; print(version('meho-connector-kit'))"
```

**When the range does not fit**, the overlay build stops. It installs the
wheels without any package index, against the kit of the base image, so a
package that does not fit the kit cannot be installed.

The kit is never published on a public package index. It ships only with
MEHO's own image and release files.

## 7. Test

Test against the MEHO release you build for:

1. Check out MEHO at the tag of that release.
2. Copy your connector folder into
   `backend/src/meho_backplane/connectors/` of the checkout, and your
   tests next to it.
3. In `backend/`, run `uv sync`, then your tests and MEHO's operation
   contract check:

   ```bash
   uv run pytest tests/test_operation_contracts.py
   ```

   The contract check reads every operation MEHO registers at start and
   checks its texts, its schemas, its risk level and its approval
   setting. It calls no vendor system.

Your tests may use MEHO's own test setup inside the checkout. That setup
is not part of the kit: it can change in any release.

## 8. Build an image

**Tier 1.** Build an overlay image from the public MEHO image, pinned by
its digest, with the script `deploy/connector-overlay/build.sh`. The
build checks every wheel, installs it, and refuses a wheel that changes a
file of the public image. The steps are in
[connector-packages.md, "Tier 1: your own image"](https://github.com/evoila/meho/blob/main/docs/codebase/connector-packages.md#tier-1-your-own-image).

**Tier 2.** A person reviews each package version with a checklist and
writes a review record. Only then does evoila build and sign the image.
The steps are in
[connector-packages.md, "Tier 2: an image evoila signs"](https://github.com/evoila/meho/blob/main/docs/codebase/connector-packages.md#tier-2-an-image-evoila-signs).

## 9. When the kit changes

The kit version has two numbers:

- the **first** number goes up when a name is removed or its inputs
  change. Your package may break. Read the notes and change your code;
- the **second** number goes up when names are only added, or a name is
  marked as old. Your package keeps working.

When the kit changed, the MEHO release notes
([CHANGELOG](https://github.com/evoila/meho/blob/main/CHANGELOG.md)) have
a section `Connector kit`. It names the new kit version. For a change in
the first number, each line says the smallest change a package must make.

A name is never removed at once. First it is marked as old, on this page
and in the release notes, with what to use instead. It may go in a later
kit version.

The full rules are in
[connector-packages.md, "The connector kit"](https://github.com/evoila/meho/blob/main/docs/codebase/connector-packages.md#the-connector-kit).
