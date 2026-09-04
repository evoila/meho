# Reference

This section is **generated from the codebase**, so it cannot drift from
the product without a red build. Each page is rendered from a registry,
command tree, or contract snapshot and committed, with a CI freshness gate
that fails the moment the source and the committed page disagree.

## Available now

- [What's new](whats-new.md) — a plain-language summary of what landed
  in recent releases, since v0.28.0.
- [Connectors](connectors.md) — the per-connector inventory: connector
  id, product, supported version range, and kind, from the connector
  registry.
- [MCP tools](mcp-tools.md) — the three-tier MCP tool surface (working,
  operator, human-only) with per-tool maturity, from the tool registry.
- [CLI](cli.md) — the `meho` command tree: every verb, its usage, and its
  flags, from the cobra command tree.
- [Feature maturity index](maturity.md) — every feature's tier, the
  milestone it targets, and where its road to GA is tracked, from the
  codebase registry.

## Landing incrementally

- REST API — from the public OpenAPI snapshot.
- Environment variables — from the backplane settings model.
- Helm values — from `values.schema.json`.
