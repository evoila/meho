# MEHO

> An MCP-native governance layer that lets any AI agent operate safely
> against shared infrastructure. Policy-gated. Audit-grade. Multi-tenant.

**Status:** v0.1 in development. No released artifact yet.

## What this is

MEHO sits between AI agents (Claude Code, Cursor, Cline, Continue,
custom MCP clients) and the infrastructure they operate against
(Kubernetes, vCenter / VCF, NSX, public cloud, network appliances,
secrets stores). Every operation is policy-gated, every credential
short-lived and federated, every result reduced server-side, every
action broadcast to a real-time feed, every interaction audited,
every context lookup tenant-scoped and version-aware.

The agent runtime is *not* part of MEHO. Bring your own.

## Status

This repository is in active development toward v0.1. There is
nothing to install yet. Watch the repo for the v0.1 announcement.

## Quickstart

(Placeholder — full v0.1 install / smoke / upgrade path lands with
the release.)

For the backplane (Python / FastAPI) skeleton — `uv` and Docker
recipes for running it locally — see [`backend/README.md`](./backend/README.md).

## Documentation

(Placeholder — `docs.meho.ai` will land before v0.1.)

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md). Contributions require a
Developer Certificate of Origin sign-off (`git commit -s`).

## Security

Vulnerability reports: see [`SECURITY.md`](./SECURITY.md).

## License

[Apache License 2.0](./LICENSE).

## History

This repository was bootstrapped on 2026-05-09 as a strategic reset.
The prior MEHO codebase lives at `evoila-bosnia/MEHO.X`, deprecated
and retained for reference.
