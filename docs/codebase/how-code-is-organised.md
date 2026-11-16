# How code is organised so pull requests do not conflict

Many pull requests are open at the same time. Two of them conflict when
both add a line at the same place, usually at the end of one shared list.
These rules stop that.

1. **One feature, one new file.** A new operation, or a small group of
   operations of one feature, goes in a new module. The module holds the
   rows (id, schemas, risk level, group, guidance), the handler and the
   preview. Adding a feature adds files. It adds no lines to a file that
   every feature edits.
2. **The package collects its modules at start-up.** The package finds its
   feature modules with `pkgutil`, as `connectors/registry.py` does. It
   reads the rows of each module and sorts them by id. There is no central
   list of modules, rows, imports or `__all__` names that each new feature
   must extend.
3. **What belongs to an operation is a field on its row.** For example its
   child calls, its preview builder, its version limits and its class (such
   as "credential write"). Never a side list in another file, keyed by
   operation id.
4. **Tests check rules, not copies.** A test reads the registry and checks
   rules: ids are unique, handlers exist, groups are known, the risk level
   fits the approval flag, examples are valid. A test never keeps a
   hand-written list of every operation, a copy of the registry or a count
   of operations. A feature's own test may pin its own rows.
5. **When a list is really needed, use one small file per entry or per
   feature, in one folder,** and read the folder. Example: the vendor API
   files that CI must find.
6. **Never merge a generated file by hand.** After you merge main, run the
   generator again. Example:
   `cd cli && make snapshot-openapi && make generate && make cli-docs`.
7. **Docs do not list or count operations.** Readers get the list from
   `list_operation_groups` and `search_operations`.
8. **Group texts stay hand-written.** They are text for the model. A new
   group adds its own entry. A new operation in an existing group changes
   nothing there.
9. **A migration takes its number last.** Merge main first, then take the
   next free number. The "Migration chain (single head)" check tells you
   when a number is taken.
10. **Old shared lists only get shorter.** A CI check,
    `backend/tests/test_shared_lists_only_shrink.py`, pins the length of
    each old shared list. If your change makes one longer, the check fails
    and says where the entry goes instead. If your change makes one
    shorter, lower its pin in the same pull request. When a list is gone,
    remove its row.

## Upgrade rows

Almost every pull request that changes behaviour adds a row to the upgrade
tables. Do not edit the tables. Add one file per row:
`docs/upgrade-rows/<version>/<short-name>.md`, with a `## Note` and an
`## Action` section of one line each (see `docs/upgrade-rows/README.md`).
`mkdocs build` adds the rows to the end of the table in
`docs-site/install/upgrades.md`. The release pull request runs
`python3 scripts/ci/upgrade_rows.py --write`, which adds them to the end of
the table in `docs/deploying.md`. The checks are in
`scripts/ci/upgrade_rows.py`: the build fails on a broken row file, and
`backend/tests/test_upgrade_rows.py` fails on a conflict marker line, a row
with the wrong number of columns, or a doubled row in either table, and when
a row of a released version (a `## [X.Y.Z]` heading in `CHANGELOG.md`) is
missing in `docs/deploying.md`.

Why not a `merge=union` line in `.gitattributes`? It works with a local
`git merge`. But GitHub does not read `.gitattributes` merge rules when it
merges on its servers (the update button, the merge queue), so the conflict
would stay there. Reference:
<https://github.com/orgs/community/discussions/9288>.

**Before you open a pull request:** merge main into your branch and run the
tests again. Then check: does your change add a line to a file that other
features also edit? If yes, move that part into your own module.

## Examples in this repo

- **Kubernetes typed operations.** Each `ops_<feature>.py` module in
  `connectors/kubernetes/` exports `OPS`. `collect_ops` in `ops.py` finds
  the modules with `pkgutil` and collects their rows, sorted by op id. The
  guard test `test_connectors_k8s_ops_collect.py` fails when a module's
  rows are not collected.
- **Operation classes.** Each connector declares the class of its own
  operations (for example "credential write") in its own package, in
  `connectors/<name>/op_classes.py`. The core file `broadcast/events.py`
  lists no operation of another connector.
- **Migration numbers.** `scripts/ci/check_migration_heads.py` runs as the
  "Migration chain (single head)" check. Its message says which number to
  take next.
