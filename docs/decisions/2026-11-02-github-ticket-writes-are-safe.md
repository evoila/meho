# GitHub ticket writes are safe (decision)

**Status:** decided by the product owner on 25 October 2026; this record
needs the project owner's approval of its pull request
**Date:** 2026-11-02
**Task:** [#4930](https://github.com/evoila/meho/issues/4930)
**Initiative:** [#5382](https://github.com/evoila/meho/issues/5382) (GitHub connector: typed issue operations and safe ticket writes)

## The decision in short

- Five GitHub writes are **`safe`** and need **no approval**
  (`requires_approval=False`):

  | Write | Operation |
  |---|---|
  | File an issue | `gh.composite.issue_create` |
  | Comment on an issue | `gh.composite.issue_comment` |
  | Link a sub-issue | `gh.composite.sub_issue_add` |
  | Add an item to a board | `gh.composite.project_item_add` |
  | Set a board field | `gh.composite.project_item_set_field` |

- They run at once only while two guards hold:
  1. **Only the target's repository and board.**
  2. **Never an infrastructure trigger.**
- Every other GitHub operation keeps its risk level. This includes the raw
  operations generated from GitHub's API file.

The product owner's words (25 October 2026): "Filing, commenting, claiming
and linking tickets need no approval, but must stay within the caller's
permitted records and must not trigger infrastructure changes." Claiming a
ticket belongs to the planning add-on (decided on 29 October 2026), so it
is not part of this decision.

## Why

- Since the approval rules of 10 October 2026, only the web interface
  counts as a person. A call with a token runs only `safe` operations at
  once. A `caution`, `dangerous` or `destructive` operation waits until a
  person clicks approve.
- A ticket write changes no infrastructure. It changes a record that people
  read.
- If a ticket write waited for approval, every agent would stop at every
  comment. Nobody would approve hundreds of comments a day with care, so the
  approval would protect nothing.
- There is a precedent: memory and knowledge writes need no approval today.

## Guard 1: only the target's repository and board

- The target names one repository in its `host`:
  `api.github.com/repos/<owner>/<repo>` (or `<owner>/<repo>`).
- The target names one board in `extras.project_id`: the node id of the
  Projects-v2 board.
- A write to another repository is refused. A board write with another
  board's node id is refused.
- A board item can only be an issue or pull request of the target's
  repository, both when it is added and when one of its fields is set.
  MEHO reads the item first; a draft issue has no repository, so its
  fields cannot be set this way.
- A sub-issue link needs the parent and the child in the target's
  repository. MEHO names the child by its number and reads it first,
  because GitHub has no call that finds an issue by its database id.
- A target that names no repository cannot run the five writes. A target
  that names no board cannot run the two board writes. The refusal says
  what to add to the target.
- MEHO refuses before it calls GitHub. The refusal is audited like any
  other failed call.
- Agent grants still decide which agent may call which operation on which
  target.

## Guard 2: never an infrastructure trigger

- MEHO never reads a ticket's state, label or comment as an instruction.
- A ticket write never starts a run, a check, an approval or an operation
  in MEHO.
- "Safe" does not mean "read" for these five. The GitHub connector declares
  them as writes and marks them as safe writes
  (`connectors/github/op_classes.py`). Only these five are on that list. So:
  - a health check (Sensor) cannot be made with one of them, and the check
    runner never runs one;
  - a satellite runner never takes one on its read path;
  - the audit row and the broadcast event still say `write`.
- The target repository must have no GitHub workflow, webhook or app that
  acts on these events: issue opened, edited, commented, labelled or
  assigned; sub-issue added; board item added or changed.
  - The probe of the target reads the workflow files in
    `.github/workflows/` and warns about each workflow that starts on such
    an event. The warning shows in the target's fingerprint.
  - The connector's rights cannot read all webhooks and installed apps. The
    operator checks them once in the repository settings
    (`docs/codebase/connectors-github.md`, "Set up a target for ticket
    writes").

## What this changes

- The five operations register `safety_level="safe"` and
  `requires_approval=False`. A test pins both fields for each one.
- `gh.composite.sub_issue_add` takes the child as `sub_issue_number`
  instead of `sub_issue_id`.
- The contract check names the five as safe writes on purpose
  (`SAFE_WRITES_ON_PURPOSE` in `backend/tests/test_operation_contracts.py`),
  with this record as the reason.
- Targets that use the board writes today need `host` set to one
  repository and `extras.project_id` set to their board. Without them, the
  board writes are refused.

## Revisit when

- GitHub adds a way for MEHO to list a repository's webhooks and installed
  apps with the connector's rights. Then the probe checks them too.
- Someone asks for another plain ticket write (for example closing an
  issue). It needs its own decision; it is not covered here.
