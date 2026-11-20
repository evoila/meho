## Note
A settings import with a token (the CLI, a script, an AI tool) that turns on `run_approval_target_kinds` or turns off `announce_gate_enabled` now gets 403, and MEHO writes nothing from that file. A preview with a token still works and names these changes. See https://evoila.github.io/meho/latest/guides/settings-file/#approval-policies-change-only-in-the-web-console.

## Action
Until the web console has a settings page, these two changes cannot be made. A stored value stays as it is. Turn the announce gate on only if you want to keep it on: it cannot be turned off again until then.
