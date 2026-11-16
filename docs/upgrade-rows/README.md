# Upgrade rows

A pull request that changes how an upgrade works adds its row to the
upgrade table as its own file here:

```
docs/upgrade-rows/<version>/<short-name>.md
```

The folder name is the version (for example `v0.48.0`). The file has two
sections, each one line of text:

```
## Note
What changes, in plain words.

## Action
What the operator does.
```

When the docs site is built, `scripts/ci/upgrade_rows.py` adds these rows
to the end of the table in `docs-site/install/upgrades.md`. The build fails
on a broken row. Run `python scripts/ci/upgrade_rows.py` to check your row
and see the table line it makes. Do not add new rows to the tables in
`docs/deploying.md` or `docs-site/install/upgrades.md`: two pull requests
that edit the end of the same table conflict.
