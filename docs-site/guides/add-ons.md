# Add-ons

An **add-on** is a separate product that runs alongside the backplane and
shares its policy, approvals, and audit. The backplane is complete on its
own; an add-on extends what a team can hand to an agent without changing
the rules an agent works under. This page explains the mechanism — how an
add-on joins the backplane and stays governed — and names the two add-ons
that exist today. It is not a setup guide for either product.

!!! note "Maturity"

    Both mechanisms on this page are **experimental** — they sit outside
    the 1.0 stability promise and may change. See the feature-maturity
    index for the pairing contract
    ([`addon_pairing`](../reference/maturity.md#addon_pairing)) and the
    attached-collection search capability
    ([`doc_collections`](../reference/maturity.md#doc_collections)).

## What an add-on is (and is not)

A paired add-on is **not** a connector. A connector is a managed vendor
system *below* the narrow waist — the agent reaches it through
`call_operation`. An add-on is a peer control plane *beside* the
backplane, integrated through the governance planes themselves. The agent
surface is untouched by pairing: an add-on's name and the surfaces it
advertises are **data** (rows in the backplane's database), never tool
names. Pairing an add-on grows no agent tools.

## The pairing contract

An add-on becomes a governed peer over a **versioned pairing contract**.
Four things follow from a successful pairing:

- **Identity.** The add-on gets its own scoped service principal from
  Keycloak. Everything it does is authorized as that identity — it starts
  with no blanket access, and every plane below attributes work back to
  the one pairing.
- **A negotiated version.** Contract versions are monotonic integers. At
  pair time both directions are pinned — the backplane refuses an add-on
  too old for it, and an add-on refuses a backplane too old for it — and
  the negotiated version is recorded. Compatibility is then re-checked
  **live**: a pairing that drifts incompatible after an upgrade or
  downgrade fails safe (its advertised surfaces deactivate until the
  versions realign) rather than half-activating silently.
- **Advertised surfaces, activated by health.** An add-on declares its
  surfaces — a meta-tool family, a CLI verb family, a console panel, an
  event kind — as data. A compatible, healthy pairing lights those
  operator-facing surfaces up, and they disappear cleanly on unpair.
  Advertising a meta-tool family registers **no** MCP tool: the agent's
  working surface stays byte-identical whether or not an add-on is paired.
- **Shared governance.** The add-on's own approval outcomes and dispatch
  completions are pushed to a durable, resumable log scoped to its
  lineage, and an out-of-process orchestration's per-step dispatches
  collapse into **one** audit-replay subtree keyed to a shared work
  reference. A multi-step add-on run therefore reads as a single lineage
  in the [audit ledger](audit-forensics.md), under the same
  [approvals](approvals-and-break-glass.md) every operation obeys.

Pairing and unpairing are reversible and audited, and a paired add-on's
health shows in the backplane's `/status`.

## The two add-ons

### Automation add-on

The backplane governs one operation at a time. The **automation add-on**
runs whole sequences of them — stand up an environment, retire one, build
a workload — as a single durable job. It runs as its own service beside
the backplane, under the scoped identity the pairing establishes, and
every mutating step it takes goes through the backplane's policy,
approval, and audit path like any other operation. It is a separate
product; this page documents only how it attaches, not how to operate it.

### MEHO Knowledge

**MEHO Knowledge** is a separate retrieval service that answers questions
over a team's own documents with a citation for every claim. The
backplane does not ingest or store any of those documents — it **attaches
to and searches** a MEHO Knowledge instance registered as a *doc
collection*, and forwards the caller's identity so the search is
authenticated and audited as that operator.

When the capability is granted, an agent gets three meta-tools:

| Tool | What it returns |
|---|---|
| `search_docs` | ranked passages from an attached collection, each cited |
| `ask_docs` | one composed answer over the retrieved passages, with its sources |
| `list_doc_collections` | the collections the caller is entitled to search |

Two properties make this safe to expose to an agent:

- **The tenant capability gate.** A docs query without a collection is
  rejected — fail-closed — and a tenant may search only the collections
  it holds the matching `meho-docs:<collection>` capability for. No caller
  can run an unscoped query or reach a collection it is not entitled to,
  and every query lands one audit row (the raw query is hashed, never
  stored).
- **Fail-closed grounding.** The grounding contract is enforced in code,
  not just in a prompt: no claim survives without a citation that resolves
  to a retrieved passage, and an empty retrieval returns a deterministic
  *"no grounded answer"* rather than a guess.

The [Memory and knowledge](memory-and-knowledge.md) guide covers how this
attached-document search differs from MEHO's own memory and knowledge
base, which live inside the backplane.

**Next:** back to the [Do real work](index.md) index.
