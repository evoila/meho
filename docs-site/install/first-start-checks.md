# The first-start checks

Check that MEHO is healthy before you use it.

1. **Wait for the first start.** On an empty database, the first start
   takes up to 15 minutes. A Pod that is `Running` but not `Ready` for
   some minutes is normal.
2. **Check the Pods and the health pages.** Step 5 of
   [the Kubernetes chapter](form-3-your-kubernetes.md#step-5-check-that-it-works)
   shows the commands. `/healthz` must answer, and `/ready` must show
   every check green.
3. **Check your own machine.** `meho doctor` names the layer that
   fails. It only reports and changes nothing. On a Linux server
   install, run `sudo meho doctor` on the server too. See
   [Troubleshooting](../clients/troubleshooting.md#start-with-meho-doctor).
4. **Sign in and read the first-start list.** MEHO checks the site and
   shows a plain list of five checks. The list needs a login. The rest of
   this page explains it.

## The first-start list

Right after the install, MEHO checks the site and shows a plain list. Each
line is green, yellow (a warning) or red with one sentence that says what to fix. The list is
the same in every install form.

The list is for people. It needs a person's login with the admin role of
the tenant, or a platform admin. An operator, a reader, and an agent,
service or runner login get the answer 403. Three ways to read it:

- The console page `/ui/first-start`. Sign in to the web console first.
- `meho status --first-start` prints the list. Run `meho login <address>`
  first. The exit code is 6 when any line is red, 2 without a login, and
  5 without the admin role.
  Add `--json` for the raw answer.
- `GET /api/v1/first-start` on the backplane, with the token of a login.

The list shows the systems of your current tenant. A platform admin sees
the systems of all tenants, and then the line names the tenant of each
system, for example `esx-2 (tenant blue)`. A red line names the systems that did not
answer. The answer holds no token, no stack trace and no internal detail.

`GET /first-start` needs no login. It runs no check and contacts no system.
It answers one fixed line that says how to sign in, and nothing about the
install. That answer has `"ok": false` and the line is `skipped`, so a
script that reads `ok` never sees green when no check ran.

The checks change nothing. The list runs at most once at a time for each
tenant: calls that arrive during a run get the result of that run, and a
finished list is kept for 10 seconds. The reachability check contacts at
most 10 systems at the same time, and it stops after 15 seconds. A system
still without an answer by then is named in the line, and the line is red.

A line is `ok` (green), `warn` (a warning), `fail` (red) or `skipped`. A warning does not make the list red, and the install goes on. A line is skipped when the
check cannot run or has no meaning: the certificate check waits until the DNS
name is green, and it is skipped when `BACKPLANE_URL` uses plain `http`. The
reachability line is skipped when some systems could not be checked (see
the notes) and none of the others failed.

## The five checks

| Check | Green when | Red line, and the fix |
|---|---|---|
| `dns` | The host of `BACKPLANE_URL` resolves, and the address it points at answers as this MEHO. | "The host name ... does not resolve." Add the name to DNS, or fix `BACKPLANE_URL`. If `BACKPLANE_URL` is empty, set it to the address people use to reach MEHO. "The address it points at does not answer as this MEHO." The name points at another server: point it at the address MEHO is served on, or fix `BACKPLANE_URL`. |
| `certificate` | MEHO opens a TLS connection to `BACKPLANE_URL`. The chain is trusted, the name matches, and more than 30 days are left. | Expired: renew the certificate. Name does not match: use a certificate that lists the name, or fix `BACKPLANE_URL`. Not trusted: install the full chain, or add your own CA to the trust bundle of MEHO. On a Linux server (form 2), the line names the command that does both: `sudo meho-host certificate --cert <chain.pem> --key <key.pem>`. Under 30 days left: renew it. Cannot connect: check that MEHO can reach its own address. |
| `network` | The host of `BACKPLANE_URL` points at a private, shared or local address. | Warning, not red: "points at a public internet address". See [MEHO stays on the management network](#meho-stays-on-the-management-network). |
| `clock` | The clock of MEHO is within 60 seconds of the time PostgreSQL reports, and of the time Keycloak reports when Keycloak sends one (see the notes). | The clock is more than 60 seconds away from Keycloak or PostgreSQL: set up time sync (NTP) on the server or the cluster nodes. No answer from Keycloak, or an error answer: check that Keycloak is running and that MEHO can reach the address in `KEYCLOAK_DISCOVERY_URL` (or `KEYCLOAK_ISSUER_URL` when that is not set). The time of PostgreSQL cannot be read: check that MEHO can reach it. |
| `reachability` | Every system in the settings answers the same call that `meho targets probe` uses. With no systems added, the line is green. | "These systems did not answer: esx-2, esx-5." Check that each one is switched on, that MEHO can reach its address, and that its login in MEHO is right. |

## MEHO stays on the management network

MEHO runs behind your firewall, on the management network. It is never on
the internet, not even the console. MEHO holds the logins for your systems,
so an open address is a risk you do not need. The `network` line warns when
the host name of `BACKPLANE_URL` points at a public internet address. This
can happen when the DNS name is wrong, or when you install behind a cloud
provider's public address. If you see the warning, point the name at a
private address of the management network, or close the public access with
a firewall, and run the checks again. The install itself is not stopped.

## Notes

- To check where the DNS name points, MEHO calls its own address
  `BACKPLANE_URL/first-start/destination` with a random value and checks the
  answer. The answer is a keyed hash under a key of this install, so another
  server, or another MEHO, cannot give it. This part of the DNS check waits
  until the certificate check is green, so a bad certificate gives one red
  line, not two.
- The certificate check uses the system trust bundle of the MEHO container.
  Point `SSL_CERT_FILE` at your own bundle if your CA is private. On a
  Linux server (form 2), `meho-host certificate` gives your CA to MEHO, and
  the check trusts it too (see [Form 2: use a certificate from your own
  CA](form-2-linux-server.md#use-a-certificate-from-your-own-ca)).
- The clock check reads the time from a successful discovery answer of
  Keycloak only. An error page from a proxy in front of Keycloak does not
  count, so the line is red while Keycloak is down.
- The time of Keycloak comes from the `Date` header of its answer. The
  Keycloak of the built-in core sends no `Date` header. Then the clock line
  compares with PostgreSQL only, says that the time of Keycloak was not
  compared, and stays green. Behind a proxy, the `Date` header comes from the
  proxy, so the line compares with the clock of the proxy.
- The reachability check does not store anything. `meho targets probe` also
  stores the detected product and version; this check only asks.
- A system whose login lives in Vault needs the token of a person signed in
  to its tenant to read that login. The list uses the token of the signed-in
  person, so it checks these systems in the current tenant. For a platform
  admin who sees all tenants, the systems of other tenants whose login lives
  in Vault are not contacted, and the line names them as not checked. That
  is not a red line. Sign in as a person to that tenant to check them, or
  run `meho targets probe <name>` there.
- A system whose login lives in Google Secret Manager is checked in every
  tenant. MEHO reads that login in one of two ways. Without
  `GSM_WIF_AUDIENCE`, it uses its own Google identity. With
  `GSM_WIF_AUDIENCE` set, it uses the token of the signed-in person, which
  Google exchanges for a short-lived one.

Next: [your first day](first-day.md).
