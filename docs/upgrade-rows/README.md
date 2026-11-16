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

A link in a row must be a full `https://` address, for example
`https://evoila.github.io/meho/latest/install/keycloak-realm/` for a docs
site page or `https://github.com/evoila/meho/blob/main/docs/deploying.md`
for a file in the repo. The same row goes into two tables in two folders,
so a relative link would work in one of them only. The check fails on a
relative link.

When the docs site is built, `scripts/ci/upgrade_rows.py` adds these rows
to the end of the table in `docs-site/install/upgrades.md`. The build fails
on a broken row. The release pull request runs
`python scripts/ci/upgrade_rows.py --write`, which adds the new rows to the
end of the table in `docs/deploying.md`. Run `python scripts/ci/upgrade_rows.py`
to check your row and see the table line it makes. Do not add new rows to
the tables in `docs/deploying.md` or `docs-site/install/upgrades.md`
yourself: two pull requests that edit the end of the same table conflict.
