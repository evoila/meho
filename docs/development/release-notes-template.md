# MEHO vX.Y.Z — `<release name>`

<One-paragraph summary a user can skim in 15 seconds. Lead with the single
biggest thing in this release — one concrete capability or fix, stated in
user terms, not commit terms.>

## Highlights

<3-5 bullets an end-user cares about. Written for the audience, not the
committers. Drop any bullet that needs the reader to know MEHO's internals
to appreciate it.>

- (highlight 1)
- (highlight 2)
- (highlight 3)

## Upgrade notes

<Breaking changes, config changes, migration steps. "None" is a valid answer
and should appear explicitly when the release is additive. If the release
requires a config flip or a migration, list the exact command.>

- (change 1)
- (change 2)

## Known limitations

<Things that don't work yet, known bugs with workarounds, platform caveats.
Surface these before evaluators find them — a known limitation with a
workaround is far less damaging than the same bug discovered under stress.>

- (limitation 1)

## Verification

<How to confirm the release is healthy after upgrade. Runnable commands where
possible; a reader should be able to copy-paste these into a terminal and
see a green signal.>

```bash
docker compose ps                  # all services Up + healthy
./scripts/validate-install.sh      # 5 steps green
curl -fsS http://localhost:8000/health
```

## Full changelog

See [CHANGELOG.md](../../CHANGELOG.md) or the compare view:
<https://github.com/evoila/meho/compare/vA.B.C...vX.Y.Z>

## Acknowledgments

<Contributors, issue reporters, external PR authors who landed changes this
cycle. Credit external contributors by GitHub handle.>
