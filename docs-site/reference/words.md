# Words we use

MEHO has some words of its own. This page explains each one in a few
plain words, in A to Z order. Each entry links to the page with more.

This page is written by hand. It is not generated from the code.

## Approval

Some changes wait until a person says yes. MEHO changes nothing on the system until then.

More: [Approvals](../can-do/approvals.md)

## Audit record

MEHO saves one row for every call. It shows who did what, to which system, and when.

More: [Audit forensics](../guides/audit-forensics.md)

## Broadcast

A shared feed where agents post what they plan, what they do, and what they finished. It helps people and agents stay out of each other's way.

More: [Broadcast](../guides/broadcast.md)

## Call an operation

Run one operation on one target. MEHO checks the rules, uses the stored login, and writes an audit record.

More: [Run your first operations](../guides/first-operations.md)

## Connector

The part of MEHO that talks to one kind of system. A generic connector is built from the vendor's API file. A typed connector is written by hand.

More: [Connectors overview](../can-do/connectors-overview.md)

## Connector version

A connector id ends with a version, for example `k8s-1.x`. It says which versions of the product the connector works with.

More: [Run your first operations](../guides/first-operations.md)

## Dashboard

A group of sensors that MEHO shows as one combined state.

More: [Watch your estate with sensors](../guides/sensors-quickstart.md)

## Detected product and version

MEHO finds the product and version of a target when it probes the target. This is also called the fingerprint. MEHO uses it to pick the connector.

More: [Register targets and secrets](../guides/targets-and-secrets.md)

## Docs collection

A set of documents that MEHO can search and answer questions from. It needs the docs add-on. A tenant can search only the collections it may use.

More: [Add-ons](../guides/add-ons.md)

## Five states

Every sensor result is one of five states: `ok`, `degraded`, `critical`, `unknown` or `skip`. `unknown` means MEHO could not judge the result. `skip` means the sensor is paused.

More: [Watch your estate with sensors](../guides/sensors-quickstart.md)

## Grant

A permission that a person gives in advance. For example, a standing grant lets a service principal run its `caution` and `dangerous` changes without a wait. A delete still waits for a person.

More: [Approvals](../can-do/approvals.md)

## Knowledge

Notes for the whole team that MEHO and agents can search, for example how a vendor API works. They stay until someone deletes them.

More: [Memory and knowledge](../guides/memory-and-knowledge.md)

## MCP

Short for Model Context Protocol. It is how AI tools connect to MEHO. MEHO gives them a short list of tools, not one tool for each system.

More: [MCP surface and scopes](../clients/mcp-surface-and-scopes.md)

## Memory

Notes that can belong to one person, one target or the whole tenant. A note can expire.

More: [Memory and knowledge](../guides/memory-and-knowledge.md)

## Operation

One thing you can do on a system, for example list the pods of a cluster. Each operation has an `op_id` and a risk level.

More: [Run your first operations](../guides/first-operations.md)

## Operation group

A set of related operations of one connector. It has a short hint on when to use it, so you can narrow your search.

More: [Run your first operations](../guides/first-operations.md)

## Operator tools

Extra tools for admin work, such as connectors, agents and schedules. A session sees them only when it asks for the `mcp:admin` scope.

More: [MCP surface and scopes](../clients/mcp-surface-and-scopes.md)

## `op_id`

The name of one operation, for example `k8s.pod.list`. You give it when you call or preview an operation.

More: [Run your first operations](../guides/first-operations.md)

## Preview

Shows what a call would send, before you run it. It sends nothing. For some operations a preview is not available.

More: [Run your first operations](../guides/first-operations.md)

## Result link

When a result is large, MEHO gives you a sample and a link to the full result. The link is also called a result handle. It expires, and only you can read it.

More: [Run your first operations](../guides/first-operations.md)

## `result_query`

The tool that reads a large result back by its result link. You can read it page by page, or ask for a filtered view.

More: [Run your first operations](../guides/first-operations.md)

## Risk level

Every operation is `safe`, `caution`, `dangerous` or `destructive`. The level helps decide what waits for approval.

More: [Approvals](../can-do/approvals.md)

## Runbook

A saved list of steps. Each step calls an operation or asks a person to do something. Each step has a check before the run goes on.

More: [Author and run runbooks](../guides/runbooks.md)

## Sandbox mode

A setting for an instance that holds only test systems that you can throw away. There, an admin may give a service principal a grant that also covers deletes.

More: [Approvals and break-glass](../guides/approvals-and-break-glass.md)

## Satellite runner

A small copy of MEHO that runs inside a network that the main MEHO cannot reach. It calls out to MEHO, runs its checks, and sends the results back.

More: [Reach isolated networks with a satellite gateway](../guides/satellite-gateway.md)

## Sensor

A check that MEHO runs on a schedule. It calls one safe operation and judges the answer by fixed rules, with no AI.

More: [Watch your estate with sensors](../guides/sensors-quickstart.md)

## Target

One system that MEHO works on, for example a Kubernetes API or a vCenter. MEHO keeps how to reach it and a reference to its login, never the password.

More: [Register targets and secrets](../guides/targets-and-secrets.md)

## Tenant

A separate space in MEHO, with its own systems and passwords.

More: [One person in several tenants](../guides/several-tenants.md)

## Working surface

The 26 tools that every MCP session sees by default. Four of them need the docs add-on, so some sessions see 22.

More: [MCP surface and scopes](../clients/mcp-surface-and-scopes.md)
