## Note
The preview of `k8s.apply` (`preview_operation`, `POST /api/v1/operations/preview` and the console preview) shows the identity of each document in `redacted_body.manifest`: one entry for each document with its kind, namespace and name, and no other content of the manifest.

## Action
A script that reads `redacted_body.manifest` of a `k8s.apply` preview reads a list of document identities, not the manifest text. No schema action.
