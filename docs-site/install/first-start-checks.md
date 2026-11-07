# First-start checks

Right after the install, MEHO checks the site and shows a plain list. Each
line is green, or red with one sentence that says what to fix. The list is
the same in every install form.

Three ways to read it:

- `meho status --first-start` prints the list. It needs no login. The exit
  code is 6 when any line is red. Add `--json` for the raw answer.
- The console page `/ui/first-start` shows the same list.
- `GET /first-start` on the backplane. It needs no login. It returns the
  lines only: no token, no stack trace, no internal detail. A red line names
  only the host from `BACKPLANE_URL` and the names of your systems.

The checks run again on every call. They change nothing.

A line is `ok` (green), `fail` (red) or `skipped`. A line is skipped when the
check cannot run or has no meaning: the certificate check waits until the DNS
name is green, and it is skipped when `BACKPLANE_URL` uses plain `http`.

## The four checks

| Check | Green when | Red line, and the fix |
|---|---|---|
| `dns` | The host of `BACKPLANE_URL` resolves. | "The host name ... does not resolve." Add the name to DNS, or fix `BACKPLANE_URL`. If `BACKPLANE_URL` is empty, set it to the address people use to reach MEHO. |
| `certificate` | MEHO opens a TLS connection to `BACKPLANE_URL`. The chain is trusted, the name matches, and more than 30 days are left. | Expired: renew the certificate. Name does not match: use a certificate that lists the name, or fix `BACKPLANE_URL`. Not trusted: install the full chain, or add your own CA to the trust bundle of MEHO. Under 30 days left: renew it. Cannot connect: check that MEHO can reach its own address. |
| `clock` | The clock of MEHO is within 60 seconds of the time Keycloak reports and the time PostgreSQL reports. | The clock is more than 60 seconds away from Keycloak or PostgreSQL: set up time sync (NTP) on the server or the cluster nodes. If a time source cannot be read, check that MEHO can reach it. |
| `reachability` | Every system in the settings answers the same call that `meho targets probe` uses. With no systems added, the line is green. | "These systems did not answer: ..." Check that each one is switched on, that MEHO can reach its address, and that its login in MEHO is right. |

## Notes

- The certificate check uses the system trust bundle of the MEHO container.
  Point `SSL_CERT_FILE` at your own bundle if your CA is private.
- The reachability check does not store anything. `meho targets probe` also
  stores the detected product and version; this check only asks.
- A system whose login lives in Vault needs a person's token to read that
  login. Without a person, MEHO cannot read it and the system shows as not
  answering. Use `meho targets probe <name>` for that system.
