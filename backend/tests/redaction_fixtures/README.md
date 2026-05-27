# Redaction round-trip fixtures (G11.4-T4 / #1073)

This directory holds **captured-raw -> expected-redacted** fixture
pairs for the round-trip CI gate. The harness lives in
`backend/tests/test_redaction_roundtrip_fixtures.py` and, for every
fixture directory listed below, re-runs the policy against `raw.json`
and asserts that the engine's output equals `expected.json` **exactly
in both directions** -- no leak (something in `raw` that should have
been redacted but isn't), and no over-redaction (something the policy
shouldn't have touched but did).

The pair is the **contract** the policy must keep across revisions.
A failure surfaces as one of:

- `extra redaction (over-redaction)` -- the engine redacted a value
  the `expected` file shows untouched. Either the policy needs a
  tighter scope, or `expected` is stale and needs regeneration.
- `missing redaction (leak)` -- the engine produced a string that
  `expected` shows redacted, but the engine output left it raw. The
  policy gained a regression: a rule was removed, a pattern was
  narrowed, or scope predicates now skip this call shape.

Both senses fail CI -- under-redaction is the safety failure, over-
redaction is the usability failure, and both are equally
load-bearing per Initiative #805's DoD.

## Layout

Each fixture is a sub-directory:

```
<fixture-name>/
  policy.yaml            # required: the policy under test
  raw.json               # required: the captured raw payload
  expected.json          # required: the expected redacted output
  manifest.json          # optional: expected manifest projection
  labels.json            # optional: { connector_id, tenant, op } for scope predicates
  README.md              # optional: 1-3 sentences on what this fixture pins
```

- `policy.yaml` is parsed via `parse_policy`. It can reference any
  named pattern in `meho_backplane.redaction.patterns.PATTERN_NAMES`.
- `raw.json` and `expected.json` are passed through `json.load`; the
  engine's input/output shape is the nested dict/list/str payload
  format. Strings are matched exactly (no whitespace normalisation,
  no key reordering).
- `manifest.json`, when present, is a list of objects with fields
  `{rule, pattern, action, count, path}`. The harness asserts these
  are equal to the engine's manifest projected onto the same fields,
  in order. `span` and `reason` are intentionally excluded from
  the projection -- the engine's docstring marks `span` as
  diagnostic-only (it indexes into the per-rule-input string, not
  the original), and `reason` belongs to the policy YAML rather than
  fixture data.
- `labels.json`, when present, supplies the `(connector_id, tenant,
  op)` triple to the engine call. Absent labels default to `None` --
  matching the "scope-less rule fires regardless" engine contract.

## Mode

The harness honours `policy.mode` (added in #1073). For
`enforce` (default) the `expected.json` is the **redacted** view;
for `shadow` it is the **unmodified** raw payload (shadow mode
emits a manifest but does not mutate). Either way, the same equality
check applies.

## Adding a new fixture

1. Capture a real raw response (sanitised of any actual secrets;
   replace with shape-equivalent dummies -- the redactor does not
   care whether the bearer token is real, only that it matches the
   regex).
2. Drop it into a new sub-directory with a descriptive name.
3. Write the policy YAML that should redact it.
4. Run the engine locally and write the expected output (or run the
   fixture suite in regen mode -- TODO when needed).
5. Commit. The CI gate will fail on the next round-trip mismatch.

## How the CI gate runs

The harness file is picked up by the standard `pytest` invocation
in `.github/workflows/ci.yml`'s `python-lint-test` job (see
`docs/codebase/redaction.md` for the gate's authoritative path).
Any round-trip mismatch fails the job and blocks merge by branch
protection.
