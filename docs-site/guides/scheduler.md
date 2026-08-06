# Schedule unattended work

The scheduler is the floor of MEHO's 24/7 operation: it fires **agent
runs** on durable triggers — a cron expression, a one-off instant, or an
event — with no operator at the keyboard. An operator (or agent author)
creates a trigger row; a background loop scans for what is due and
launches the bound agent definition when its time comes. This is what
runs your nightly reconciliation, your scheduled remediation, and the
autonomous writes behind the [agent-requester approval
pattern](approvals-and-break-glass.md#option-1-the-agent-requester-pattern-recommended).

The hard part of unattended execution is not the timer — it is *credentials*:
a scheduled run days later still needs a live identity to reach Vault.
This guide covers creating triggers and the durable-credential behavior
that keeps them firing.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and an **agent definition** to fire
      (`meho agent create …`).
    - Creating and cancelling triggers needs **tenant_admin**; listing
      needs **operator**.
    - The scheduler is **Beta** — see the
      [feature-maturity index](../reference/maturity.md#scheduler). Its
      road to GA is tracked in
      [#2668](https://github.com/evoila/meho/issues/2668), the same
      issue that hardened the durable-credential behavior below.

## The surface at a glance

| Action | MCP tool | CLI |
|---|---|---|
| List triggers | `meho_scheduler_list` | `meho scheduler list` |
| Create a trigger (tenant_admin) | `meho_scheduler_create` | `meho scheduler create …` |
| Cancel a trigger (tenant_admin) | `meho_scheduler_cancel` | `meho scheduler cancel <trigger_id>` |

## Create a trigger

Every trigger binds one `--agent-definition` and one `--kind`. A
**cron** trigger fires repeatedly on a 5-field expression evaluated in
its persisted `--timezone`:

```bash
meho scheduler create --kind cron \
  --agent-definition nightly-reconcile \
  --cron-expr "0 2 * * *" --timezone Europe/Vienna \
  --inputs '{"scope": "prod"}' \
  --work-ref gh:evoila/meho#520
```

A **one-off** trigger fires once at a stored instant, then transitions
to a terminal state:

```bash
meho scheduler create --kind one_off \
  --agent-definition drain-and-patch --fire-at 2026-08-10T22:00:00Z
```

A third kind, **event**, fires an agent run in response to a backplane
event (`--event-filter`) rather than a clock. The knobs worth knowing:

- `--inputs` — a JSON object rendered into the agent's prompt at fire
  time.
- `--identity-sub` — the subject the run acts under (this is what makes
  a scheduled run park its writes under an *agent* principal, distinct
  from your own, so four-eyes stays intact — see the
  [approvals guide](approvals-and-break-glass.md)).
- `--in-flight-policy` — what to do when a fire is due while the
  previous run of the same trigger is still going.
- `--work-ref` — a change-ticket reference stamped onto the run's audit
  and broadcast lineage.

List what's scheduled and its state:

```bash
meho scheduler list
```

Each `ScheduledTrigger` row carries `kind`, `cron_expr` / `fire_at`,
`timezone`, the hot `next_fire_at` column the loop scans, `last_fired_at`,
`status`, and — when a fire was skipped — a `last_skip_reason` and a
`skip_count`.

## Durable credentials: surviving unattended

A scheduled run has **no operator logged in**, so there is no Keycloak
JWT to forward to Vault's OIDC auth method. The scheduler instead sources
the agent's `client_credentials` secret **Vault-first**, under its own
static service token (`VAULT_SCHEDULER_TOKEN`), from
`SCHEDULER_AGENT_VAULT_PATH_PATTERN` (default
`secret/data/agents/{client_id}/credentials`), falling back to a pod env
var only when Vault yields nothing.

That static token is the failure point unattended operation used to hit:
a periodic Vault token **dies in the field** — it ages out, or an
operator forgets to re-mint it — and every credential read then `403`s,
so the scheduler **silently skips its fires**. A job that ran for weeks
just stops, quietly. The hardening shipped with the
[#2668](https://github.com/evoila/meho/issues/2668) line closes that
hole three ways:

- **Self-heal instead of skip.** On a `lookup-self`-confirmed dead
  token, the scheduler mints a **fresh** Vault token by `jwt_login` as
  the runner principal (runner JWT + `VAULT_CHECK_RUNNER_ROLE`, falling
  back to `VAULT_OIDC_ROLE`) and retries the failed read once — no
  operator, no sidecar in the loop. If the re-mint itself fails it falls
  back to the existing **loud** failure, never a silent skip.
- **Renewal on a timer, not on traffic.** Token renewal now fires on a
  dedicated tick cadence rather than only when agent-secret traffic
  flows — so an *idle* scheduler no longer ages its token out between
  jobs.
- **A loud pre-death guard.** Startup and an hourly `lookup-self` log a
  loud `ERROR` (`scheduler_vault_token_will_expire`) when the token is
  non-renewable or carries an explicit max TTL — it will die despite
  renewal, so it must be minted `-period=768h`.

When credentials genuinely cannot be resolved (no self-heal path
provisioned), the loop does the honest thing: it logs
`scheduler_credentials_unresolved`, **skips** the fire, and records
`last_skip_reason='credentials_unresolved'` with an incremented
`skip_count` on the trigger row — so `meho scheduler list` shows you
exactly why a job isn't running, instead of leaving you to guess.

!!! note "Provisioning the headless mint"

    The headless `client_credentials` → runner-JWT → Vault `jwt_login`
    mint (the identity the self-heal logs in as) is provisioned once,
    deploy-side. The recipe lives in
    [`docs/cross-repo/vault-provisioning.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/vault-provisioning.md);
    the dedicated `VAULT_CHECK_RUNNER_ROLE` lets you bound exactly what
    that background identity can read.

## Scheduler and the checks runner

The scheduler fires **agent runs**. It is not the same background worker
as the **checks/sensors runner**, which evaluates deterministic
[sensors](sensors-quickstart.md) on their own cadence — but both are
"background work with no operator present", and both depend on the same
durable-credential story. The sensor side of it (the `checkRunner.*`
chart block and `VAULT_CHECK_RUNNER_ROLE`) is covered in
[Sensors and your credential backend](sensors-quickstart.md#sensors-and-your-credential-backend);
this page is its agent-run twin.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| A trigger shows `last_skip_reason=credentials_unresolved` and a rising `skip_count` | The scheduler could not resolve the agent's credentials and skipped the fire (loudly, not silently). | Stage the agent's `client_credentials` secret at `secret/data/agents/{client_id}/credentials`, or provision the self-heal mint (`vault-provisioning.md`). |
| Startup logs `scheduler_vault_token_will_expire` (ERROR) | `VAULT_SCHEDULER_TOKEN` is non-renewable or has an `explicit_max_ttl` — it will die despite renewal. | Re-mint it as a periodic token: `vault token create -period=768h …`. |
| `403 insufficient_role` on `scheduler create` / `cancel` | Creating and cancelling triggers need **tenant_admin**; listing is operator. | Use a tenant_admin session to author triggers. |
| A cron trigger never fires | The `--cron-expr` didn't parse, or `--timezone` is wrong, so `next_fire_at` was never set to a reachable instant. | Check `meho scheduler list` for `next_fire_at`; fix the 5-field expression / IANA timezone and recreate. |
| A scheduled write is stuck `awaiting_approval` | Working as designed — an autonomous run's `requires_approval` write parks under the agent's subject. | Approve it as yourself; because the requester is the agent, four-eyes is satisfied ([approvals](approvals-and-break-glass.md)). |
| Overlapping runs of the same trigger | A fire came due while the previous run was still going. | Set `--in-flight-policy` to the behavior you want (skip / queue) at create time. |

**Next:** [Satellite gateway](satellite-gateway.md).
