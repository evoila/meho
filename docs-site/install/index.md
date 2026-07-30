# Install & operate

!!! note "Stub — content tracked by [evoila/meho#2671](https://github.com/evoila/meho/issues/2671)"

This section will carry a **single continuous install trail** from
empty cluster to running backplane, plus day-2 operation:

- Prerequisites and sizing.
- Helm install (chart at `oci://ghcr.io/evoila/meho-chart`).
- Credential-backend decision: Vault vs Google Secret Manager.
- Keycloak realm setup — bootstrap automation and the manual reference.
- TLS and ingress.
- Upgrades, backup / restore, observability.

Until it lands, the install material lives in-repo:
[`docs/deploying.md`](https://github.com/evoila/meho/blob/main/docs/deploying.md)
and
[`deploy/values-examples/`](https://github.com/evoila/meho/tree/main/deploy/values-examples).
