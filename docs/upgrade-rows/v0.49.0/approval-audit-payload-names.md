## Note
The payload of an approval audit row names each person in a pair of id and name: `principal_sub` and `principal_name` are the person who acted on the row (the same person as the row's user id column), `requester_sub` and `requester_name` are the person who asked, and an approve or reject row also has `reviewed_by` and `reviewed_by_name`. Rows written before the upgrade keep their stored form. The audit API, the CLI, the MCP audit tools and the console show such a row with the row's user id as `principal_sub` and the requester's id as `requester_sub`.

## Action
If a script or a log export reads `payload.principal_sub` of approval rows to find the person who asked, change it to read `payload.requester_sub`.
