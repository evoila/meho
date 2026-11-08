# Common problems

- **Login fails, or a client cannot connect:**
  [Troubleshooting auth walls](../clients/troubleshooting.md).
- **`/ready` answers 503 and `/healthz` is green:** MEHO may not trust a
  certificate. See [TLS and ingress](tls-ingress.md#the-backplanes-own-trust-internal-ca-bundle).
- **A Keycloak login problem:**
  [If login fails](keycloak-realm.md#if-login-fails).
- **MEHO is ready but cannot reach a target:** the network rules block
  it. See [Let MEHO reach the systems it manages](form-3-your-kubernetes.md#let-meho-reach-the-systems-it-manages).
- **A check of your own machine:** run `meho doctor`.
