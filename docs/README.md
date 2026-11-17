# `docs/` — contributor-facing documentation

This directory holds **contributor-facing** material: architecture decision records (ADRs), runbooks, codebase guides, and design notes. It lives in the repository so it can evolve with the code and be reviewed through pull requests.

## The two documentation trees

MEHO maintains two separate doc trees. Please file changes in the correct one:

| Tree | Path | Audience | What it contains |
|---|---|---|---|
| **Contributor docs** | `docs/` | Developers & maintainers | ADRs, runbooks, codebase guides, design notes |
| **Published operator docs** | `docs-site/` | Operators & end users | Install, setup, task guides, reference |

## Where to file changes

- **ADRs:** Add a new numbered markdown file under `docs/decisions/`. See the existing ADRs for format and the SPDX header convention (`SPDX-License-Identifier: Apache-2.0`).
- **Runbooks / codebase guides:** Add a markdown file under `docs/` (top level) or an appropriate subdirectory.
- **Operator-facing install / setup / task docs:** Edit the corresponding file under `docs-site/` instead. That tree builds the published site at https://evoila.github.io/meho/.

## Relationship to `docs-site/`

`docs-site/` is a separate mkdocs project that builds the versioned, published documentation site. The `docs/` tree in this repo is **not** published directly — it is reference material for people working on the MEHO codebase itself.

## Contributing

All contributions require a Developer Certificate of Origin sign-off (`git commit -s`) — there is no CLA. See the root [`CONTRIBUTING.md`](../CONTRIBUTING.md) and [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md).
