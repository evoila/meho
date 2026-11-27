## Note
With `POLICY_CLASSIFICATION` or `CUSTOM_POLICY` on, the policy names a connector with no version by its product, the id a call uses (for example `vault`, not `-`). A policy file entry now matches the operations of such a connector when its `connector` pattern matches the product. An entry that matched the old form `-` (for example `*-*`) still matches them. So no call waits less, and a call can now wait where an entry names the product. The carry-over dry run, `meho policy explain`, the bulk edit and the overrides report (`meho connector overrides`) name such a connector by its product, and the dry run lists one line per product. No database migration.

## Action
**Nothing to do** while both switches are off. Otherwise check the entries whose `connector` pattern names a product of a connector with no version: they now make its operations wait or classify them. Run `meho policy carry-over --dry-run` to see which operations each "always wait" entry matches.
