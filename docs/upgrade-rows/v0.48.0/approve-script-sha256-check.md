## Note
`meho approvals approve <id>` for a request that runs a script now shows the script's SHA-256 and asks first. From a pipe it approves only when the matching SHA-256 is given. Several ids and `--in-order` follow the same rule, and an id given twice is refused.

## Action
Change any script that approves such a request: pass the reviewed SHA-256 on standard input, or approve in a terminal or on the console.
