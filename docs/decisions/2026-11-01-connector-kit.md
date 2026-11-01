# The connector kit for connector authors: one module, one version, one check (decision)

**Status:** Agreed on 2026-11-01 by the program owner.
**Date:** 2026-11-01
**Issue:** [#5278](https://github.com/evoila/meho/issues/5278) — the connector kit as a documented package for connector authors
**Initiative:** [#5060](https://github.com/evoila/meho/issues/5060) — a connector built in another repository runs like a built-in one
**Builds on:** [2026-10-28-own-connectors-leave-the-public-tree.md](2026-10-28-own-connectors-leave-the-public-tree.md)
and [connector-packages.md](../codebase/connector-packages.md) (the start-up check, the overlay build, the review before signing)

## The decision in short

- MEHO gets one small Python package for connector authors: **`meho-connector-kit`**.
  Its one module, `meho_connector_kit`, offers the 30 names a connector package may use.
  Every other MEHO module is private. It can change in any release, without notice.
  The kit ships only with MEHO's own release files and image, never on a public package index.
- The kit has **its own version number**, starting at 1.0. The first number goes up
  when a change can break a package. The second number goes up when a name is added.
- A connector package says which kit versions it fits, in the normal Python way:
  `dependencies = ["meho-connector-kit>=1.0,<2"]`. The overlay build (the build that adds
  connector packages to the public MEHO image) refuses a package that does not fit. MEHO
  checks again when it starts.
- A test writes down every kit name with its inputs. A change makes the test fail until
  the list is written again and the kit version goes up. The release notes then get a
  `Connector kit` section.
- A template folder in this repository, `templates/connector-package/`, gives authors a working
  start. One page in the docs site explains the kit, and the README points to it.

## Context

Short code paths, such as `connectors/base.py` or `connector_packages.py`, are under
`backend/src/meho_backplane/`.

- Today the kit is the whole `meho-backplane` package. A connector package can import any
  module, and nothing tells the author which imports are safe (#5278).
- The connector packages that exist today live in a private evoila repository: evoila's
  Fortinet package (three connectors) and a made-up example. Their code imports 31 names
  from 16 MEHO modules. Their tests import 43 names from 22 modules. We counted this on
  1 November 2026 by reading the files, without running them.
- Six of the 16 modules are private by their name (a part of their path starts with an
  underscore): four in `connectors/_shared/`, plus `operations/_preview.py` and
  `operations/_validate.py`.
- Some names sit in places that do not fit a promise:
  - `ConnectorAuthError` means "the vendor refused the credential". The dispatcher (the part
    of MEHO that runs an operation) treats it as that general signal, and connectors for other
    vendors use it. But it lives in `connectors/_shared/vcf_auth.py`, a module named for
    VMware Cloud Foundation.
  - `PreviewBuilder` and `PermissionPreflight` are not in the `__all__` list of
    `operations/_preview.py` (lines 93-107).
  - The type `Target` is only `Any` (`connectors/base.py`, line 35). Nothing says which target
    fields a connector may read.
- Nothing says which MEHO a package fits:
  - The packages name no dependency on MEHO in their `pyproject.toml`. Their CI tests against
    one MEHO release tag, written in a file.
  - The `meho-backplane` package has the version `0.1.0-dev` in every image
    (`backend/pyproject.toml`, line 3), and the release guide keeps it so
    ([RELEASING.md](../RELEASING.md), lines 79-80). So a range on `meho-backplane` cannot work.
  - MEHO ships often: six minor releases between 23 October and 1 November 2026
    (`CHANGELOG.md`). Before 1.0, a minor release may break things (RELEASING.md, lines 89-92).
    A range on MEHO release numbers would need a new package version almost every week. And
    each new wheel (a built package file) needs a new review by a person before evoila signs it
    ([connector-packages.md](../codebase/connector-packages.md), "The review record").
- Only the release pull request edits `CHANGELOG.md` (RELEASING.md, lines 191-193). So the pull
  request that changes the kit cannot write its own CHANGELOG line.

## The decision

### 1. The module list

A connector package imports MEHO only through **`meho_connector_kit`**. The names in the table
are the promise. Every other MEHO module is private to the core.

**Choice: one new module, not a list of today's modules.** Reason: six of today's modules are
private by their name, and some names sit in a module named for one vendor. With one module,
the core can move code without breaking a package, the check has one place to look, and an
author has one import line. The thing a package depends on (`meho-connector-kit`) is also the
thing it imports (`meho_connector_kit`).

The list is the smallest that today's packages need: the 31 names they use, minus `dispatch`
and `get_broadcast_client` (see "Left out" below), plus `Target`.

| Group | Name | Today in | What a package uses it for |
|---|---|---|---|
| Register | `register_connector_v2` | `connectors/registry.py` | Register the connector class under its product, version and implementation id. |
| Register | `register_typed_op_registrar` | `operations/typed_register.py` | Queue the function that registers the operations when MEHO starts. |
| Register | `register_typed_operation` | `operations/typed_register.py` | Register one hand-written operation: the shape of its inputs and of its answer, its risk level, whether it waits for approval, and its texts for agents. |
| Register | `EmbeddingService` | `retrieval/embedding.py` | The type of the `embedding_service` value that MEHO hands to the registering function. The package passes it on unchanged. |
| Connector | `Connector` | `connectors/base.py` | The base class of every connector. |
| Connector | `HttpConnector` | `connectors/adapters/http.py` | The base class for an HTTP API. It brings MEHO's checks for TLS, addresses, redirects and answer size. |
| Connector | `Target` | new in the kit (`Any` today) | The target fields a connector may read. |
| Answers | `FingerprintResult`, `ProbeResult`, `OperationResult` | `connectors/schemas.py` | The answers of `fingerprint`, `probe` and `execute`. |
| Answers | `AuthModel` | `connectors/schemas.py` | How a target logs in. |
| Errors | `ConnectorUpstreamError` | `connectors/base.py` | The vendor answered with an error status. |
| Errors | `ConnectorAuthError` | `connectors/_shared/vcf_auth.py` | The vendor refused the credential. |
| Errors | `CredentialsReadError`, `VaultCredentialsReadError` | `connectors/_shared/credential_backend.py`, `connectors/_shared/vault_creds.py` | MEHO could not read the target's secret. |
| Errors | `VaultClientError` | `auth/vault.py` | MEHO could not log in to Vault for the caller. |
| Caller | `Operator` | `auth/operator.py` | The person or agent who called. A package passes it on. |
| Caller | `synthesise_system_operator`, `is_system_operator` | `connectors/_shared/system_operator.py` | MEHO's own caller, for checks that run without a person. |
| Secrets | `load_vault_secret_data` | `connectors/_shared/vault_creds.py` | Read the target's secret from Vault, as the caller. |
| Secrets | `load_param_secret_data` | `connectors/_shared/vault_creds.py` | Read a secret that an operation input names, after MEHO checks that the caller may name it. |
| Secrets | `strip_credential_value` | `connectors/_shared/vault_creds.py` | Clean a secret value, for example remove a new line at the end. |
| Secrets | `is_acceptable_auth_model` | `connectors/_shared/vcf_auth.py` | Check that the target uses the shared service account login. |
| Secrets | `target_cache_key` | `connectors/_shared/cache_key.py` | A cache key that keeps tenants apart. |
| Approvals | `PreviewContext`, `PreviewBuilder`, `register_preview_builder` | `operations/_preview.py` | Show a person what a change will do, while the change waits for approval. |
| Approvals | `PermissionPreflight`, `register_permission_preflight` | `operations/_preview.py` | Check, while a change waits, that the caller may make it. |
| Audit | `compute_audit_digest_hash` | `operations/_validate.py` | A hash made with a secret key of MEHO, for a value that lands in an audit or approval row. |

Inside three classes, the kit promises these parts:

- `HttpConnector`: `__init__`, `auth_headers`, `aclose`, `trace_response_body`, `_get_json` and
  `_post_json`. The last two start with an underscore, but subclasses call them, so they are
  part of the kit.
- `PreviewContext`: `target`, `operator`, `params`, `connector_instance` and `connector_id`. The
  field `descriptor` is a MEHO database row. Its fields stay private.
- `Target`: `name`, `host`, `extras`, `auth_model`, `verify_tls` and `secret_ref`. These are the
  fields today's packages read. Both kinds of target that MEHO hands to a connector have them:
  the database row (`db/models.py`) and the read model (`targets/schemas.py`).

**Left out, and why:**

- `operations.dispatch`. The packages call it only inside `Connector.execute`, to run the
  operation again as MEHO's own caller. But MEHO never calls `execute` for a hand-written
  operation. The dispatcher calls the function that `register_typed_operation` recorded
  (`operations/typed_register.py`, module text). No code outside the connector folders calls
  `execute`. So `Connector.execute` gets a default answer, and a package no longer writes it.
- `broadcast.client.get_broadcast_client`. One package uses it to keep a little state that every
  MEHO process must see, in MEHO's shared Valkey store (an in-memory store). The same store holds
  MEHO's broadcast feed, rate limits and approval waits (`connectors/result_handle_store.py`,
  module text). Decision 2: no shared store for connectors now.
- The result handle store (`connectors/result_handle_store.py`). No package needs it. A handler
  returns plain data, and MEHO turns a long list into a result handle (a link to the stored
  result) after the handler returns ([jsonflux.md](../codebase/jsonflux.md)).

A name joins the kit when a package needs it, through the check below. The VMware and Azure
packages move on the tier 2 path, so they may also import modules outside the kit (decision 1).

### 2. The check

How a change to the kit is noticed:

1. **The name list.** The file `backend/connector-kit/kit-names.json` lists every kit name: what it
   is (a class, a function or a value), and its inputs with their types and defaults. For a
   class, it lists the parts the kit promises. The file also holds the kit version.
2. **The test.** `backend/tests/test_connector_kit.py` builds the same list from the code and
   compares it with the file. When they differ, the test fails. Its message shows the
   difference, says which number of the kit version must go up, and names the command below.
   The test also fails when one of MEHO's two kinds of target loses a field of `Target`.
3. **The command.** `backend/scripts/write_connector_kit_names.py` writes the file again and
   raises the kit version:
   - the first number, when a name is removed or its inputs change;
   - the second number, when names are only added.

   It writes the same version into `backend/connector-kit/pyproject.toml`. The test checks that
   both versions are the same.
4. **The pull request** shows the changed list file, so a person sees the kit change before the
   merge. The kit owner must approve it, through the code owners file (decision 3).
5. **The release.** A small script in `scripts/release/` compares the list file at the last release
   tag with the release commit. When the kit changed, it checks two things:
   - the version went up by the right number;
   - the release notes have a `### Connector kit` section that names the new kit version. For a
     change in the first number, each line says the smallest change a package must make, as
     the `Breaking changes` lines of `CHANGELOG.md` do today.

   [RELEASING.md](../RELEASING.md) runs the script in step 2, where the CHANGELOG is rolled.

**Choice: a test with a list file, as #5278 suggests.** Reason: it needs no new tool, it runs in
the normal unit test job, and the file shows each change in the pull request. A change to a kit
module always gets the full test run, because all kit names live in core modules, `_shared/` or
`adapters/` (`backend/tests/_unit_connector_select.py`).

### 3. Versions

- **The kit version** is the version of the `meho-connector-kit` package. Every MEHO image carries
  it next to `meho-backplane`. **Choice:** the first version is 1.0, not 0.1. Reason: today's
  packages already use these names, so the promise holds from the first day.
- **A package states its range** in its `pyproject.toml`, for example
  `dependencies = ["meho-connector-kit>=1.0,<2"]`. The wheel carries it as a normal Python
  dependency.
- **At build.** The overlay build installs the wheels with `uv pip install --offline --no-index`
  (`deploy/connector-overlay/Dockerfile`, lines 46-48). So uv checks the range against the kit in
  the base image, without any package index. We tried this on 1 November 2026 with made-up
  wheels and the same options: a range that fits installs, and a range that does not fit stops
  the install with "No solution found ... cannot be used". The build adds one check before the
  install: a connector wheel that names no kit range stops the build, and the message names the
  package.
- **At start.** The start-up check (#5034) already reads each listed package before MEHO imports
  any connector folder (`connector_packages.py`, `_listed_folders`). It also reads the package's
  range and compares it with the kit in the image. A package with no range, or with a range that
  does not fit, stops the start with `connector_package_kit_mismatch`. The message names the
  package, its range and the kit version of the image. The package itself does nothing.
  **Choice:** stop the start, as an unknown folder does today. Reason: the build already refuses
  this case, so it only happens with an image made in another way. A clear stop is better than a
  package whose operations fail later.
- **Imports outside the kit.** The range covers the kit only. Only a tier 2 package (evoila's own)
  may import other MEHO modules. It is pinned to one exact MEHO version and tested on every MEHO
  release (decision 1). A tier 1 package, like the template's sample, imports only the kit.
- **Where the kit comes from.** The kit is never published on a public package index. It ships only
  with MEHO's own release files and image. evoila's private packages build against the kit of one
  pinned MEHO release tag, and install it from MEHO's own release files. A connector package gets
  into MEHO only through the overlay build, which reads no package index.
- **MEHO's release number keeps its meaning.** It stays the version of the image, the chart and the
  CLI. `meho-backplane` stays `0.1.0-dev`.

### 4. The template folder

The template is the folder `templates/connector-package/` in this repository. An author copies the
folder into a repository of their own and renames the made-up names.

```text
templates/connector-package/
  README.md                  how to start, tier 1 and tier 2, how to test against a MEHO release
  pyproject.toml             the wheel: name, version, kit range, the one folder it holds
  meho_backplane/connectors/example_widget/
    __init__.py              registers the connector, its operations and its preview
    connector.py             ExampleWidgetConnector, an HttpConnector
    operations.py            the two operations and their texts
  tests/
    __init__.py
    test_example_widget.py   both operations through MEHO, against a fake widget API
    test_kit_imports.py      the package imports MEHO only through the kit
```

**The sample connector** uses made-up names:

- Package `example-connectors`, folder `example_widget`, connector id `example-widget-1.x`
  (product `example`, implementation `example-widget`, version `1.x`). It talks to a made-up
  "widget" API.
- It imports only `meho_connector_kit`. It reads its token with `load_vault_secret_data`.
- It has two operations:
  - `example.widget.list`: a safe read that returns a long list, so MEHO answers with a result
    handle;
  - `example.widget.rename`: a change that waits for a person's approval, with a preview.

  The proof Task #5040 needs a long list, a denied call and a waiting approval. The made-up
  package of that proof becomes this sample connector (#5564).
- `tests/test_kit_imports.py` reads the package's files without running them, the same way
  `backend/tests/test_split_ready_imports.py` does. It fails when a file imports a MEHO module
  other than the kit or the package's own folder.

**CI:** one small job in this repository's CI tests the sample against this repository's own code.
There is no pinned MEHO tag, and nothing to move after a release. The job:

1. copies the sample's folder and its tests into the CI checkout, and checks that MEHO's connector
   list has `example-widget-1.x`;
2. runs the sample's tests (with `tests/test_kit_imports.py`) and MEHO's operation contract check
   (`backend/tests/test_operation_contracts.py`);
3. builds the sample's wheel and runs the wheel checks of `deploy/connector-overlay/overlay_check.py`.

So a kit change that breaks the sample fails in the same pull request. The folder is not part of the
MEHO image or of any MEHO package.

**Tier 2:** evoila's private repository tests its packages against one pinned MEHO release tag, and
builds the overlay image from the image of that tag. Its release adds `--push --sign --reviews` to
the overlay build ([connector-packages.md](../codebase/connector-packages.md), "Tier 2: an image
evoila signs").

### 5. The docs page

One page: **`docs-site/extend/connector-kit.md`**, with the title "Write a connector package". It
sits in a new menu section, "Extend MEHO", in `mkdocs.yml`. **Choice:** a new section, because
"Do real work" is for operators who run MEHO, not for authors who extend it.

The sections of the page:

1. **What you build.** A wheel with one folder under `meho_backplane/connectors/`. MEHO runs its
   operations with the same policy, approvals, audit, secret removal and result handles as its own.
   Tier 1 and tier 2.
2. **Start from the template folder.** Copy `templates/connector-package/` into a repository of your
   own, and rename the made-up names.
3. **The connector kit.** The module, the kit version, and every name in a table, grouped as above.
   Everything else is private.
4. **Write the connector.** The class, the target fields, secrets and errors.
5. **Add operations.** The inputs of `register_typed_operation`, risk levels and approvals, the texts
   for agents, and a preview for a change. What MEHO does for you: policy, audit, secret removal
   and result handles.
6. **Say which MEHO releases fit.** The kit range, where to find the kit version of a MEHO release,
   and what happens when the range does not fit.
7. **Test.** Test against the MEHO release you build for: its operation contract check, and tests
   that run inside a MEHO checkout without a promise.
8. **Build an image.** The overlay build for tier 1; the review and the signature for tier 2.
9. **When the kit changes.** The `Connector kit` section of the release notes, and what the two
   numbers of the kit version mean.

The page links to the codebase pages instead of repeating them:
[connector-packages.md](../codebase/connector-packages.md),
[connectors-shared-vault-creds.md](../codebase/connectors-shared-vault-creds.md),
[approvals.md](../codebase/approvals.md), [jsonflux.md](../codebase/jsonflux.md) and
[error-message-shape.md](../codebase/error-message-shape.md). The kit test also checks that every
kit name and the kit version are on the page, so the page cannot fall behind the code.

**The README.** The `README.md` of this repository gets a short section, "Writing a connector
package". It says in three lines what the kit is, and links to this page and to the template
folder.

## Consequences

- **Connector authors** write one import line and one range. A package that does not fit stops at
  the build, with a clear message.
- **MEHO developers** can change code outside the kit freely. A change to a kit name fails one
  test. They run one command, and the pull request shows the change.
- **The release** gets one more check. The release notes get a `Connector kit` section when the
  kit changed.
- **evoila's packages** add a range and drop their own `execute`. On the tier 2 path they may keep
  imports outside the kit, pinned to one exact MEHO version and tested on every release
  (decision 1). This happens in the private repository.
- **The proof #5040** gives the template folder its sample connector.
- **The image** carries one more small Python package. `meho-backplane` and MEHO's release numbers
  do not change.

## Out of scope

- The overlay build, the allowlist file, the review and the signature (#5034, #5035, #5039;
  [connector-packages.md](../codebase/connector-packages.md)). This record adds only the range check
  to the build and to the start.
- Multi-step operations and operations from an API file. No package uses them yet. Their names join
  the kit when one does.
- Test helpers and CI scripts. Package tests run inside a MEHO checkout at the pinned tag and may use
  MEHO's own test setup. That carries no promise.
- CLI commands. A package's operations are reached with `meho operation call` and the MCP tool
  `call_operation`, like every other connector. A package adds no command.

## Decisions on the open questions

The program owner answered the five open questions on 1 November 2026.

1. **Imports outside the kit: only on the tier 2 path.** evoila's own packages may import MEHO
   modules outside the kit. Such a package is pinned to one exact MEHO version and tested on every
   MEHO release. The public template path (tier 1) gets the kit only. The VMware and Azure moves use
   the tier 2 path. Today the first-wave VMware folders import 158 names from 54 MEHO modules, and
   `azure` imports 42 names from 22 (counted the same way, on `main` on 1 November 2026).
2. **No shared store for connectors now.** The kit has no store. The one package that keeps state in
   MEHO's own store does so outside the kit, on the tier 2 path.
3. **A kit change needs the kit owner's approval, through the code owners file.** `.github/CODEOWNERS`
   names the kit owner for `backend/connector-kit/`. The kit owner is the program owner until the
   program owner names someone else.
   - The file takes effect only when the rules of `main` ask for a code owner's review. Today they
     do not (`require_code_owner_review` is off). Turning it on is an admin step.
   - GitHub does not count an approval by the person who opened the pull request. So a kit change
     opened under the kit owner's own account needs another code owner to approve it.
4. **The template is the folder `templates/connector-package/` in this repository.** It holds the
   sample connector. A small CI job tests it against this repository's own code, so there is no
   pinned MEHO tag and nothing to move after a release.
5. **An old kit name stays for one kit version, marked as old, then goes.** The list file and the docs
   page mark the name as old, and the release notes say what to use instead. The check of section 2
   refuses to remove a name that was not marked as old in the kit version before.

## Tasks that follow

Under #5060, on 1 November 2026: #5278 was rewritten for coding, and #5563, #5565 and #5564 were
filed.

- **#5278, the coding step: the kit and its check.**
  - `backend/connector-kit/`: `pyproject.toml` (`meho-connector-kit`, version 1.0) and the module
    `meho_connector_kit` with the 30 names. `Target` lists the six fields as a typed class.
  - The image build installs the kit next to MEHO (`backend/Dockerfile`), and `uv sync` installs it
    for the tests.
  - `Connector.execute` gets a default answer: an error that says to use a registered operation.
  - The list file, the command and the test of section 2. The release script and its step in
    RELEASING.md.
  - A section "The connector kit" in [connector-packages.md](../codebase/connector-packages.md).
  - The code owners file for `backend/connector-kit/` (decision 3).
  - No change to `CHANGELOG.md`. The next release pull request adds `Connector kit` to the list of
    CHANGELOG sections.
- **#5563: MEHO checks the kit range at build and at start** (section 3), in
  `deploy/connector-overlay/overlay_check.py` and `connector_packages.py`. The made-up example in
  `deploy/connector-overlay/example/` names a range. The range test runs in the overlay build itself,
  with its pinned uv.
- **#5565: the docs page** `docs-site/extend/connector-kit.md`, its menu entry, the test that every
  kit name and the kit version are on it, and the README section "Writing a connector package".
- **#5564: the template folder** `templates/connector-package/` and its CI job (section 4). The
  made-up package of the proof #5040 becomes its sample.
- **In the private repository, not filed here:** evoila's packages name their kit range and drop their
  own `execute`. On the tier 2 path they may keep other imports (decision 1).

**Order:** #5278 first. Then #5563, #5565 and #5564. The private repository moves after #5563.

## Not proven yet

- The range test ran with uv 0.12.0 on a laptop. The overlay build pins uv 0.11.12
  (`deploy/connector-overlay/Dockerfile`, line 28). #5563 repeats it in the build.
- That the kit installs next to MEHO in the image build. The install step installs every wheel in
  `/wheels/` without dependencies (`backend/Dockerfile`, line 126). #5278 shows it.

## Words used here

- **Connector package:** a wheel (a built Python package file) that adds connector folders under
  `meho_backplane/connectors/`.
- **Connector kit:** the MEHO names a connector package may use. They are in one module,
  `meho_connector_kit`.
- **Kit version:** the version of the `meho-connector-kit` package, for example 1.2. It is not the
  MEHO release number.
- **Range:** the kit versions a package fits. `>=1.0,<2` means version 1.0 or later, but before 2.
- **Overlay image:** a new image built from the public MEHO image, with connector packages added.
- **Tier 1 and tier 2:** tier 1 is a user's own image, which evoila does not sign. Tier 2 is an image
  with evoila's own packages, reviewed by a person and signed by evoila (#5060).
- **Result handle:** a link to a stored result. MEHO uses it for long lists.
