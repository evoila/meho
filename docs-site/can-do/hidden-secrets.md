# Secret values stay hidden

An agent or a person does not see a password, a token or a key in the
result of an operation.

## What it does

MEHO hides secret values in three places:

- **In answers.** Every operation hides the value of each secret-named
  field in its answer, for example `password`, `token`, `api_key` or
  `client_secret`. This holds for the result, for the sample rows, and
  for the rows behind a result handle. Secret reads show the names of
  the keys, not their values.
- **In previews and approvals.** The preview of a change that waits for
  approval hides secret values. In a link, MEHO hides the user and
  password part and the values of signature, credential and token
  parameters. A decided approval request keeps no secret values.
- **In traces and records.** The flight recorder, the audit log, error
  text from a vendor and broadcast messages hide secret values too. The
  audit row keeps a keyed fingerprint instead of the value. Two equal
  values still give the same fingerprint, so you can see a repeat
  without seeing the secret.

More rules:

- MEHO also finds secrets in free text, for example private key blocks
  and labels such as `DB_PASSWORD=`.
- A person or a service principal can ask for the real value with
  `reveal_secret: true`. An agent is refused.
- An operation that makes a new credential still gives it to the
  caller once.
- A connector can mark a field that only looks secret, such as a date
  or a count, so that it stays whole.

## Product versions

This works for every connector and every product version. It is a rule
of the MEHO core, not of one connector.

## State

- **Shipped** from v0.40.0, and extended in each release up to v0.45.0:
    - v0.40.0: secret reads show names, not values.
    - v0.42.0: every operation hides secret-named fields in its answer.
    - v0.43.0: a decided approval request keeps no secret values.
    - v0.44.0: more values are hidden in answers, previews and records.
    - v0.45.0: link secrets are hidden in approvals and traces. The
      Azure connector masks keys in traces.
- The [What's new](../reference/whats-new.md) page has the details of
  each release.

## Operator guide

- [Flight recorder](../guides/flight-recorder.md): what a trace holds
  and how it hides secrets.
- [Approvals and break-glass](../guides/approvals-and-break-glass.md):
  what an approver sees.
- [Register targets and secrets](../guides/targets-and-secrets.md): how
  MEHO reads a secret from your password safe.
