## Note
`k8s.apply` refuses a manifest text whose documents together are larger than 3145728 characters (3 MiB) as JSON, with their YAML aliases and merge keys written out, with `invalid_params`, on the call and on the preview. Each part counts at least its length in JSON: brackets, separators, quotes and escapes too.

## Action
Split a larger manifest into several calls. No schema action.
