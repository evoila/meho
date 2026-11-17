# Upgrade rows

A pull request that changes how an upgrade works adds its row to the
upgrade table as its own file here:

```
docs/upgrade-rows/<version>/<short-name>.md
```

The folder name is the version of the next release (for example
`v0.47.0`). A folder of a version that is already released takes no new
row. The file has two sections, each one line of text:

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
to the end of the table in `docs-site/install/upgrades.md`, in this order:
by version, then by file name. The build fails on a broken row. The release
pull request runs `python scripts/ci/upgrade_rows.py --write`, which adds
the rows of the release to the end of the table in `docs/deploying.md`, in
the same order. Run `python scripts/ci/upgrade_rows.py`
to check your row and see the table line it makes. Do not add new rows to
the tables in `docs/deploying.md` or `docs-site/install/upgrades.md`
yourself: two pull requests that edit the end of the same table conflict.
The check fails on such a row.

To fix a typo in a row of a released version, change the row file and the
same row in `docs/deploying.md` in the same pull request.
