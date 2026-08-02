# Project

How the MEHO project itself is run: what carries a stability promise,
how versions and deprecations work, how to report a vulnerability, and
where the roadmap lives.

## Feature maturity

Every feature carries an explicit **GA / Beta / Experimental** tier,
declared once in the feature-maturity registry and rendered on every
surface (MCP tool descriptions, REST OpenAPI, CLI help, console
badges). The full index — every non-GA feature, its gaps, target
milestone, and tracking issue — is generated from the registry and
lives at [Feature maturity index](../reference/maturity.md).

## Versioning and deprecation policy

MEHO follows [SemVer](https://semver.org). The version lives only in
the release tag; the backplane image, Helm chart, CLI tarballs, and
this docs site all derive from it
([release history](https://github.com/evoila/meho/releases)).

Pre-1.0, breaking changes still ship in minors — each one carries a
**migration recipe** in the release notes (the smallest concrete edit
a v(N−1) client makes to keep working on v(N); see the *Breaking
changes* convention in the
[CHANGELOG](https://github.com/evoila/meho/blob/main/CHANGELOG.md)).
The formal 1.0 stability promise — frozen contract surfaces, CI
compatibility gates, and a deprecation window policy — is being built
under [evoila/meho#2662](https://github.com/evoila/meho/issues/2662)
and will be documented here when it lands.

## Security policy

Report vulnerabilities per
[`SECURITY.md`](https://github.com/evoila/meho/blob/main/SECURITY.md)
— coordinated disclosure, no public issue for an unpatched finding.
Release artefacts (image, chart, CLI tarballs) are cosign-signed
keyless under a common identity-claim format; verification commands
ship in the release notes.

## Roadmap

The road to v1.0.0 is tracked as
[evoila/meho#2661](https://github.com/evoila/meho/issues/2661):
fresh-user install from these docs, Claude Desktop connectivity, the
clean-room evaluation program, and the contract freeze. The goal map
is public — `gh issue list --repo evoila/meho --label goal`.

## Contributing

Start with
[`CONTRIBUTING.md`](https://github.com/evoila/meho/blob/main/CONTRIBUTING.md).
Every commit needs a DCO `Signed-off-by` line (`git commit -s`).
