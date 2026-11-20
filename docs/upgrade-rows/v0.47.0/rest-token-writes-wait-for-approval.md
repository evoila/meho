## Note
A person's REST write of a target or of the topology graph made with a token (the `meho` CLI, scripts) waits for approval. `POST`, `PATCH` and `DELETE /api/v1/targets`, `POST /api/v1/topology/edges`, `POST /api/v1/topology/edges/bulk`, `DELETE /api/v1/topology/edges/{edge_id}` and `DELETE /api/v1/topology/nodes/{node_id}` answer 202 with the id of the approval request. Nothing is written until a person approves it. Only a person in the web console writes at once.

## Action
**Upgrade the `meho` CLI together with the backplane.** An older CLI reads the 202 answer as done and reports the write, but nothing is written yet. Tell CLI and script users that target and topology writes wait for approval: they approve them in the web interface or with `meho approvals approve`. `meho targets import` makes one approval request per row.
