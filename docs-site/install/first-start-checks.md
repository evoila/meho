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
4. **Read the first-start list.** MEHO checks the site and shows a plain
   list of five checks. The rest of this page explains it.

## The first-start list

Right after the install, MEHO checks the site and shows a plain list. Each
line is green, yellow (a warning) or red with one sentence that says what to fix. The list is
the same in every install form.

Three ways to read it:

- `meho status --first-start` prints the list. It needs no login. The exit
  code is 6 when any line is red. Add `--json` for the raw answer.
- The console page `/ui/first-start` shows the same list.
- `GET /first-start` on the backplane. It needs no login. It returns the
  lines only: no token, no stack trace, no internal detail. A red line names
  only the host from `BACKPLANE_URL`. The reachability line shows how many
  systems did not answer, not their names. The console page shows the names.

Calls that arrive while a run is in progress get the result of that run.
After a run ends, `GET /first-start` and `meho status --first-start` show
its result for 30 seconds, and then the next call runs the checks again.
So after you fix a problem, wait 30 seconds before you look again. The
console page runs the checks on every load. The checks change nothing.

A line is `ok` (green), `warn` (a warning), `fail` (red) or `skipped`. A warning does not make the list red, and the install goes on. A line is skipped when the
check cannot run or has no meaning: the certificate check waits until the DNS
name is green, and it is skipped when `BACKPLANE_URL` uses plain `http`. The
reachability line is skipped when some systems need a signed-in person (see
the notes) and none of the others failed.

## The five checks

| Check | Green when | Red line, and the fix |
|---|---|---|
| `dns` | The host of `BACKPLANE_URL` resolves, and the address it points at answers as this MEHO. | "The host name ... does not resolve." Add the name to DNS, or fix `BACKPLANE_URL`. If `BACKPLANE_URL` is empty, set it to the address people use to reach MEHO. "The address it points at does not answer as this MEHO." The name points at another server: point it at the address MEHO is served on, or fix `BACKPLANE_URL`. |
| `certificate` | MEHO opens a TLS connection to `BACKPLANE_URL`. The chain is trusted, the name matches, and more than 30 days are left. | Expired: renew the certificate. Name does not match: use a certificate that lists the name, or fix `BACKPLANE_URL`. Not trusted: install the full chain, or add your own CA to the trust bundle of MEHO. Under 30 days left: renew it. Cannot connect: check that MEHO can reach its own address. |
| `network` | The host of `BACKPLANE_URL` points at a private, shared or local address. | Warning, not red: "points at a public internet address". See [MEHO stays on the management network](#meho-stays-on-the-management-network). |
| `clock` | The clock of MEHO is within 60 seconds of the time Keycloak reports and the time PostgreSQL reports. | The clock is more than 60 seconds away from Keycloak or PostgreSQL: set up time sync (NTP) on the server or the cluster nodes. If a time source cannot be read, check that MEHO can reach it. |
| `reachability` | Every system in the settings answers the same call that `meho targets probe` uses. With no systems added, the line is green. | "2 of 5 systems did not answer." (the console page names them). Check that each one is switched on, that MEHO can reach its address, and that its login in MEHO is right. |

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
  Point `SSL_CERT_FILE` at your own bundle if your CA is private.
- The clock check reads the time from a successful discovery answer of
  Keycloak only. An error page from a proxy in front of Keycloak does not
  count, so the line is red while Keycloak is down.
- The reachability check does not store anything. `meho targets probe` also
  stores the detected product and version; this check only asks.
- A system whose login lives in Vault needs a person's token to read that
  login. The console page uses the token of the signed-in person, so it checks
  these systems too. The public list and `meho status --first-start` have no
  person: they do not contact these systems, and the line says how many
  systems could not be checked. That is not a red line. Sign in to the console
  to check them, or run `meho targets probe <name>`.

Next: [your first day](first-day.md).
