## Note
`k8s.apply` refuses a manifest document that is larger than 3145728 characters (3 MiB) with its YAML aliases and merge keys written out, with `invalid_params`, on the call and on the preview.

## Action
Split a larger document into smaller objects. No schema action.
