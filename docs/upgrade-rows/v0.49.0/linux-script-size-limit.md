## Note
linux.script.run refuses a script over 1,048,576 bytes (1 MiB) before it waits for approval. The approver reads a longer script page by page.

## Action
Keep scripts at or under 1 MiB, for example copy a large file to the host first. Requests that wait at upgrade time show no page links in the console; read their pages with `meho approvals show <id> --script-page N`. A request with a script over 1 MiB that waits at upgrade time is refused when it is approved. Send it again with a shorter script.
