# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 2.x     | :white_check_mark: |
| < 2.0   | :x:                |

## Reporting a Vulnerability

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report them via email to **security@evoila.com**.

You should receive a response within 48 hours. If for some reason you do not, please follow up via email to ensure we received your original message.

Please include the following information:

- Type of vulnerability (e.g., SQL injection, XSS, SSRF, authentication bypass)
- Full path of the affected source file(s)
- Steps to reproduce the issue
- Proof of concept or exploit code (if available)
- Impact assessment

## Disclosure Policy

- We will acknowledge receipt within 48 hours
- We will confirm the vulnerability and determine its impact within 7 days
- We will release a fix within 30 days of confirmation
- We will publicly disclose the vulnerability after the fix is released

## Scope

The following are in scope:
- MEHO backend API (`meho_app/`)
- MEHO frontend (`meho_frontend/`)
- Docker images (`ghcr.io/evoila/meho-backend`, `ghcr.io/evoila/meho-frontend`)
- Connector integrations (credential handling, API calls)

The following are out of scope:
- Third-party services MEHO connects to (Kubernetes, VMware, etc.)
- The meho.ai marketing website
- Social engineering attacks

## Recognition

We appreciate responsible disclosure and will credit reporters in our release notes (unless you prefer to remain anonymous).
