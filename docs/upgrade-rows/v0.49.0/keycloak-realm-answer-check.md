## Note
Every Keycloak operation now reads the target's managed realm first and checks the realm name in Keycloak's answer. The name must be exactly the `managed_realm` of the target: the same letters, the same letter case and no space around it. MEHO refuses a Keycloak target whose `managed_realm` is not exactly the realm's name in Keycloak, also when only the letter case differs. The check runs before the call waits for approval, before `preview_operation` answers, and again when the call runs. A refused call stores no approval request and sends nothing more to Keycloak.

## Action
For each Keycloak target, compare its `managed_realm` (`meho targets describe <target> --json`) with the realm name that the Keycloak admin console shows. If they differ, also only in letter case, correct the realm name on the target: run `meho targets update <target> --extra managed_realm=<the name exactly as Keycloak shows it>`.
