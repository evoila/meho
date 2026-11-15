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
- `trigger.json`: the event trigger. It matches `check.dashboard.changed`
  events that are worse and are not known noise.

## Set it up

The agent needs its own login first (a Keycloak client). Register it with the
starting grants "read everything":

```bash
meho agent-principal register checks-investigator --grants read-everything
```

The client id is `agent:checks-investigator`. The grant lets the agent run
`safe` operations at once on every target in this tenant, also on targets
added later. The agent only reads. Anything else it tries waits until a
person approves it. Without a grant, and with the server setting
`AGENT_GRANT_REQUIRED=on`, the agent could run nothing. See the
[hosted agents guide](../../docs-site/guides/hosted-agents.md#starting-grants-pick-them-when-you-create-the-agent).

Then run two commands from this folder. The agent already has a grant, so
`meho agent create` needs no `--grants`.

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

Check the agent's grant with
`meho agent grant list --principal agent:checks-investigator`.

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
