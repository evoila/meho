## Note
With the setting `CUSTOM_POLICY` on, a tenant admin can store rules for personal agents in the tenant's policy file (`agents`), inside the outer limit of the install. The import plan shows, for each role, the operations that would start to run at once. No call reads the rules in this version. With the setting off, a file with `agents` is refused with "needs CUSTOM_POLICY". A roll-back to v0.48 is safe: v0.48 reads a stored policy key by key, so it ignores a stored `agents` part, its export leaves the part out, and its next import of the tenant's file removes it.

## Action
None.
