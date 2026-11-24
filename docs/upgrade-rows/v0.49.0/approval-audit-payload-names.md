## Note
The payload of an approval audit row names each person in a pair of id and name: `principal_sub` and `principal_name` are the person who acted on the row (the same person as the row's user id column), `requester_sub` and `requester_name` are the person who asked, and an approve or reject row also has `reviewed_by` and `reviewed_by_name`. Rows written before the upgrade keep their stored form. The audit API, the CLI, the MCP audit tools and the console show every such row of an approval request with the row's user id as `principal_sub` and the requester's id as `requester_sub`, also when the requester acted on the row. A read straight from the database finds the requester of such a row in `payload.principal_sub`. On an approve row, `payload.self_approved` tells whether the reviewer was the requester.

## Action
If a script or a log export reads `payload.principal_sub` of approval rows to find the person who asked, change it to read `payload.requester_sub`.
