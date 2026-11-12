<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Checks investigator: one agent and one event trigger

This example starts an agent when a check Dashboard gets worse. The agent
finds the cause and answers with a short finding. MEHO stores the finding and
mails it to the Dashboard's mail address.

It needs `CHECKS_AGENT_EVENTS=true` (off by default). You can change the agent,
copy it for another Dashboard, or turn the trigger off. Nothing here is fixed
in MEHO.

## Files

- `agent.json`: the agent (system prompt, tools, turn budget).
- `permissions.json`: what the agent may do. It reads freely. Every change
  waits for a person to approve it.
- `trigger.json`: the event trigger. It matches `check.dashboard.changed`
  events that are worse and are not known noise.

## Set it up

The agent needs its own login first (a Keycloak client). Register it as in the
[R1 guide](../r1-tiered-triage/GUIDE.md), with the client id
`agent:checks-investigator`. MEHO cannot make this login for you.

Then run two commands from this folder.

```bash
# 1. Create the agent.
jq .toolset agent.json | meho agent create checks-investigator \
  --identity-ref "agent:checks-investigator" \
  --model-tier deep \
  --turn-budget 25 \
  --system-prompt "$(jq -r .system_prompt agent.json)" \
  --toolset @-

# 2. Create the trigger.
meho scheduler create \
  --kind event \
  --agent-definition "$(meho agent show checks-investigator --json | jq -r .id)" \
  --event-filter @<(jq .event_filter trigger.json) \
  --identity-sub "agent:checks-investigator" \
  --in-flight-policy fail_into_audit
```

Then give the agent its grants. Post each row of `permissions.json` with
`meho agent grant create --principal <sub> --op <pattern> --target '*' --verdict <verdict>`.

## What happens

1. A Dashboard gets worse. MEHO groups the failing Sensors by their shared
   cause and publishes one event for each group.
2. The trigger starts the agent once for each group. A second event for the
   same group waits while the first run is still working.
3. When the run ends with a finding, MEHO stores it in memory
   (`checks-noise-<group key>`) and mails it to the Dashboard.
4. If the finding says `re_escalate: false`, the next change for the same
   group is published with `known_noise: true`. The trigger does not match it.

The old investigator path in MEHO steps aside when this trigger exists, so one
Dashboard change never starts the agent twice. See
`docs/codebase/checks-investigator.md`, "Two paths".
