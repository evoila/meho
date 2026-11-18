## Note
MEHO now refuses a broadcast announcement whose `activity`, `target`, an entry of `targets`, or `scope` holds a line break, a tab or another control character: `POST /api/v1/broadcast/announce` answers 422 and the MCP tool `meho_broadcast_announce` answers -32602. In its text output (without `--json`) the CLI shows control characters in text that an agent or another person wrote as visible escapes. `meho kb show`, `meho conventions show`, `meho runbook start` and `meho runbook next` keep the line breaks of a body, but print a tab as `\x09`. The `--json` output is unchanged.

## Action
Make sure that agents and scripts that announce on the broadcast feed send each text field as one line without tabs. A script that pipes the text output of `meho kb show`, `meho conventions show`, `meho runbook start` or `meho runbook next` and needs the exact bytes of a body now gets `\x09` for each tab: switch it to `--json` and read the body field there.
