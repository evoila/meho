## Note
Before v0.47.0, a probe of an OpenBao server stored the product as `vault`. Since v0.47.0 the OpenBao connector serves only a target whose stored product is `openbao`. An OpenBao 2.x target probed before the upgrade matches no connector, and every `vault.*` call on it fails with `no_connector`, even though its detected version looks right. The error now says what to do.

## Action
Probe every OpenBao target again after the upgrade: `meho targets probe <name>`. The probe finds `openbao` and stores it, and calls work again.
