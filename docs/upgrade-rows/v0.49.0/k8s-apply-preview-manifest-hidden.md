## Note
The preview of `k8s.apply` (`preview_operation`, `POST /api/v1/operations/preview` and the console preview) shows the manifest in `redacted_body.manifest` with the same hiding as the approval card: one entry for each document, with the same hidden marks.

## Action
A script that reads `redacted_body.manifest` of a `k8s.apply` preview reads a list of documents, not the manifest text. No schema action.
