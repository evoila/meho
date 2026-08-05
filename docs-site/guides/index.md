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

## Coming to this section

- Later guides — topology, broadcast, memory / knowledge, audit
  forensics, runbooks, scheduler, satellite gateway — land as the
  evaluation program shows where the pain is.

!!! tip "When a guide and your deploy disagree"

    These guides are written against the product version this site
    version documents (see the version selector). If a command or
    error message differs on your deploy, check that your CLI and
    backplane versions match the docs version you are reading —
    then [file a docs issue](https://github.com/evoila/meho/issues):
    a guide a fresh user cannot execute verbatim is a bug.
