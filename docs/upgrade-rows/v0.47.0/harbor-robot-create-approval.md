## Note
**Action needed.** A person must approve each `harbor.robot.create` call. A standing grant or a run approval that names `harbor.robot.create` no longer covers it: after the upgrade, each call waits until a person approves it.

## Action
Tell the teams whose agents or runbooks make Harbor robots that each call now waits for a person. An agent must give `vault_target` with a path inside the tenant's Vault path (for example `vault:tenants/<tenant_id>/ci/harbor#secret`), so MEHO stores the robot secret in Vault.
