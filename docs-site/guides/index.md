# Do real work

You have a running backplane and a connected client. This section is
the bridge from "connected" to "operating real infrastructure": each
guide is a task-shaped, start-to-finish walk you can execute against a
fresh deploy.

Work through the first three in order — each builds on the previous
one:

| Guide | What you will have at the end |
|---|---|
| [Register targets and secrets](targets-and-secrets.md) | Your systems registered as targets, credentials staged in your secret store, and a green probe proving MEHO can reach and identify each one. |
| [Run your first operations](first-operations.md) | The discover → groups → search → preview → call ladder, exercised end to end against a real target, including how to page large results and what the safety flags mean. |
| [Watch your estate with sensors](sensors-quickstart.md) | A sensor evaluating a real check on a schedule, a dashboard rolling it up, and a triage walk of the five-state vocabulary when something breaks. |

Then, when an operation needs a second pair of eyes:

| Guide | What you will have at the end |
|---|---|
| [Approvals and break-glass](approvals-and-break-glass.md) | A clear path through the four-eyes rule, including the two ways a **single operator** clears a gated write — the recommended agent-requester pattern and the audited emergency break-glass — and how to prove to an auditor which one your deploy runs. |

## Go deeper

Once the core loop is second nature, these guides cover the rest of the
backplane — reach for whichever the task in front of you needs:

| Guide | What it covers |
|---|---|
| [Topology](topology.md) | The dependency graph: blast-radius (`dependents`), reachability (`path`), and how a resource gets into the graph. |
| [Broadcast](broadcast.md) | The cross-operator activity feed — read it, announce intent, watch it live, and the read-before-start discipline. |
| [Memory and knowledge](memory-and-knowledge.md) | Two durable stores: scoped, TTL'd memory and the tenant knowledge base, and which belongs where. |
| [Audit forensics](audit-forensics.md) | Reconstructing "who did X to Y and when" from the append-only ledger, and tracing an agent session end to end. |
| [Flight recorder](flight-recorder.md) | The redacted per-dispatch record of the vendor traffic behind an audit row — what it captures, and who can read it back. |
| [Runbooks](runbooks.md) | Authoring versioned, multi-step procedures and running them one gated step at a time. |
| [Scheduler](scheduler.md) | Firing agent runs on cron / one-off triggers, and the durable-credential behavior that keeps them running unattended. |
| [Event ingestion](event-ingestion.md) | Firing a governed agent run from an authenticated external webhook — a monitoring alert or a registry action. |
| [Satellite gateway](satellite-gateway.md) | Remote check execution for networks the central instance cannot dial. |
| [Add-ons](add-ons.md) | How a separate product pairs with the backplane and stays under its policy, approvals, and audit. |

!!! tip "When a guide and your deploy disagree"

    These guides are written against the product version this site
    version documents (see the version selector). If a command or
    error message differs on your deploy, check that your CLI and
    backplane versions match the docs version you are reading —
    then [file a docs issue](https://github.com/evoila/meho/issues):
    a guide a fresh user cannot execute verbatim is a bug.
