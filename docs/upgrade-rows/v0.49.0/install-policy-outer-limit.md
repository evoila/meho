## Note
With the setting `CUSTOM_POLICY` on, a platform admin can store the outer limit for personal agents in the install's policy file (`limits.personal_agent_up_to`); with it off, a file with `limits` is refused with "needs CUSTOM_POLICY". A roll-back to v0.48 is safe: v0.48 reads a stored policy key by key and does not check it against the file model, so it ignores a stored `limits` part, its export leaves the part out, and its next import of the install's file removes it.

## Action
None. To use the limit, turn on `CUSTOM_POLICY` and import the install's file with `meho policy import --scope install`.
