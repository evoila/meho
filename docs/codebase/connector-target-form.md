# Connector target form declarations

## Overview

A connector class can say what a target of its product needs. It declares
four optional class attributes on `Connector`
(`backend/src/meho_backplane/connectors/base.py`). The core only reads them.
It does not change how a connector logs in. The console uses them to build
the target form and the credential form.

| Attribute | Meaning |
|---|---|
| `default_port` | The usual port of the product, or `None`. |
| `target_fields` | The extra settings of a target (keys in its `extras`), or `None`. |
| `credential_ways` | The ways to log in and the fields of the secret for each. `None` means not declared. An empty tuple means "declared: no credential needed". |
| `test_operation` | The op id of one `safe` read that needs no input beyond the target. `None` means not declared. |

A class that sets none of them behaves as before.

Declared today: the Linux SSH connector (`LinuxSshConnector`, two ways: key
and password; both also take the known hosts text), Harbor (one way: user name and password) and Kubernetes (one
way: kubeconfig). Other SSH connectors do not declare yet, and nothing is
declared on the shared `SshConnector` class.

## Key types

All in `backend/src/meho_backplane/connectors/target_form.py`. They are
frozen dataclasses. A connector package imports `TargetField`,
`CredentialField` and `CredentialWay` from `meho_connector_kit` (kit 1.6 or
higher).

- `TargetField`: `key`, `label`, `help`, `required`, `kind` (`text`,
  `number`, `boolean` or `choice`), `choices`, `default`.
- `CredentialField`: `name` (the field name in the secret), `label`, `help`,
  `required` (default yes), `hidden` (default yes), `multiline` (for a key or
  a kubeconfig).
- `CredentialWay`: `key`, `label`, `fields`.
- `ConnectorDeclarationError`: raised for a broken declaration. The message
  names the connector class.

Names are 1 to 200 characters, the same rule as the names in a secret field
map.

## Control flow

1. At start, `operation_catalog_write` writes the operation rows. Then
   `assert_connector_declarations_valid`
   (`backend/src/meho_backplane/operations/connector_declaration_check.py`)
   runs in the same transaction, before the release stamp.
2. For every registered class (once, also when it has two registry keys) it
   checks the shape: a blank name, a name twice in one way, a way key twice, a
   target field key twice, a `choice` with no choices, a port outside 1 to
   65535.
3. For a class with a `test_operation` it reads the built-in operation row of
   the same product, version and implementation. The row must exist and have
   risk level `safe`. A call with no input (`{}`) must pass the same parameter
   check a real call runs: the schema as calls see it (with its `oneOf` and
   `anyOf` rules), checked by `validate_params`, then the op's own params
   check. So a read that needs one of two inputs fails the start too.
4. A failure raises `ConnectorDeclarationError`. The start fails and the
   transaction rolls back, like any other start-up registration failure.

The route `GET /api/v1/connectors/target-form?product=<product>`
(`backend/src/meho_backplane/api/v1/connectors_target_form.py`) returns the
declarations of every class of one product, each with its `impl_id`, version
and version range. Role: operator. It answers 404 with a plain message for an
unknown product. The caller sees every hand-written class, because it is
installed with MEHO. A class made by an import counts only when the caller may
see that connector (`connector_triples_visible_to` in
`operations/_lookup.py`): the import is built in, or it belongs to the
caller's tenant. A connector that another tenant imported answers 404. It holds names and labels only. It never reads the secret
store. The router is included before the `/{connector_id}` routes of the
ingest router, so `target-form` is not read as a connector id.

## Dependencies

- `connectors/registry.py` for the registered classes
  (`connectors_for_tenant` for the route).
- `operations/_validate.py` and `operations/switch_params.py` for the
  parameter check of the test read.
- The `endpoint_descriptor` rows written by the typed-operation registrars.
- `cli/api/openapi.json` and the generated CLI client change with the route.

## Writing a declaration

```python
from meho_connector_kit import Connector, CredentialField, CredentialWay


class MyConnector(Connector):
    product = "my-product"
    credential_ways = (
        CredentialWay(
            key="basic",
            label="User name and password",
            fields=(
                CredentialField(name="username", label="User name", hidden=False),
                CredentialField(name="password", label="Password"),
            ),
        ),
    )
    test_operation = "my-product.project.list"
```

Use field names that the connector really reads from its secret. Pick a test
read that needs the login, so it proves the login works.

## References

- `backend/tests/test_connector_target_form.py`
- `docs/codebase/targets.md`
- `docs/codebase/api-shape-conventions.md`
