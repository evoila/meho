## Note
A new switch `CUSTOM_POLICY` (Helm `config.policy.customPolicy`), off by default. With it on, the three switches `POLICY_CLASSIFICATION`, `POLICY_ENGINE_SHADOW` and `POLICY_APPROVER_ROUTING` count as on. Each of the three still works alone. `GET /ready` shows it as `features.policy.custom_policy`. With it off, nothing changes. See https://github.com/evoila/meho/blob/main/docs/codebase/policy.md.

## Action
None. Leave `CUSTOM_POLICY` off for now; later releases put the custom policy model behind it.
