<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `docs/cross-repo/` — cross-repository coordination specs

Specifications of the contracts `evoila/meho` exchanges with sibling
repositories. Every doc in this directory describes a **handshake** that
crosses a repo boundary: what `evoila/meho` produces, what the consumer
side must provision, and how each side verifies the contract holds.

These docs are upstream-side **trackers**, not the consumer's
implementation. The consumer-side code, secrets, and infrastructure live
in the partner repo. What lives here is the spec the consumer reads to
know what to build, and the verification commands either side can run to
prove the handshake works end-to-end.

## Current handshakes

| Doc | Consumer repo | Surface |
| --- | --- | --- |
| [`rke2-infra-coordination.md`](./rke2-infra-coordination.md) | [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc) | Per-PR ephemeral-cluster smoke + `repository_dispatch` deploy trigger; cluster auth (OIDC > kubeconfig); namespace-scoped RBAC for `meho-ci-*` |

## When to add a doc here

A handshake belongs in `docs/cross-repo/` when **all** of the following
are true:

1. The contract is between two distinct GitHub repositories (not two
   directories of one repo).
2. One side produces a stable interface (an event, a workflow trigger, a
   kubeconfig consumer, an OCI artefact) and the other side consumes it.
3. The contract has identifiable acceptance criteria on *both* sides —
   "we send `X`" + "they receive `X` and do `Y`".

If the cross-repo edge is a single comment in code or a single field in
a values file, put the note next to the code instead. This directory is
for the contracts substantial enough to need their own page.

## Related

- `docs/codebase/` — durable internal architecture docs (per area:
  backend, cli, devops). These describe what's inside `evoila/meho`;
  `cross-repo/` describes what crosses out of it.
- Each handshake doc carries a status table that this README does not
  duplicate — drift between the two would be a bug.
