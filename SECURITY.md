# Security policy

## Reporting a vulnerability

Please report security vulnerabilities to **security@meho.ai**, NOT
via public GitHub issues.

We acknowledge reports within 5 business days and aim to provide
a substantive response within 15 business days.

## Scope

In scope:

- Code in this repository
- The default Helm chart and Compose configurations once they ship

Out of scope:

- Third-party MCP clients (Claude Code, Cursor, Cline, etc.)
- Customer-managed dependencies (Vault, Keycloak, etc.) deployed
  alongside MEHO

## Disclosure

We follow coordinated disclosure. We will work with the reporter
on a fix and an advisory; please give us reasonable time to address
the issue before public disclosure.

### Publication vehicle

Advisories are published as **GitHub repository security advisories
(GHSA)** on `evoila/meho`:
<https://github.com/evoila/meho/security/advisories>. A vulnerability
in MEHO's own code, Helm chart, or release workflows is not considered
disclosed until its advisory is published there. Upstream CVEs that
MEHO merely patches (base-image packages, Python or npm dependencies)
are covered by the upstream project's advisory; MEHO records the bump
in `CHANGELOG.md` under `### Security` and publishes no GHSA of its
own.

### Coordinated-disclosure steps

1. **Report received** at security@meho.ai; acknowledged within
   5 business days (see above). Triage happens on the private
   maintainer tracker, never in a public issue.
2. **Private draft advisory.** A maintainer opens a draft GHSA on
   `evoila/meho` (Security → Advisories → New draft), records the
   affected versions and severity (CVSS 3.1), and adds the reporter
   as a collaborator so they can review the text.
3. **Fix in a temporary private fork.** The draft's temporary private
   fork holds the fix; it is reviewed and tested there so the patch
   does not appear on `main` before the advisory is ready.
4. **CVE request (optional).** For a vulnerability that adopters should
   track in their scanners, we request a CVE through the GHSA form;
   GitHub is the CNA and typically assigns within ~72 hours.
5. **Publish together.** The fix merges, the fixed version is tagged,
   the `CHANGELOG.md` `### Security` entry names the fix, and the GHSA
   is published in the same release cycle — with credit to the
   reporter if they consent.
6. **Ledger.** Every `### Security` entry in `CHANGELOG.md` has a row
   in [`docs/security/advisory-ledger.md`](docs/security/advisory-ledger.md)
   recording its class and whether an advisory was published, is not
   applicable (upstream CVE, hardening with no vulnerability, docs), or
   was exempted with a recorded rationale. The release runbook
   ([`docs/RELEASING.md`](docs/RELEASING.md)) reconciles that ledger
   before each release-candidate and GA tag, so no security fix ships
   without a disclosure decision on record.

Reference: GitHub Docs — [About repository security advisories](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/about-repository-security-advisories).

## Hall of fame

Confirmed reporters who consent will be acknowledged in our
SECURITY-thanks.md once it exists.
