## Note
Adding a check to a dashboard that already has a state no longer sends a false alarm. MEHO waits for the new check's first confirmed result, but not forever. If the check has no confirmed result by its first due time (at most one day after the check was made), plus the time its result confirmation takes (with the run time of each run), plus 60 seconds, MEHO alerts as `degraded`. It also alerts when the check is more than 60 seconds overdue. A check that already had a confirmed state does not wait. A first result of `unknown` now also counts as a confirmed state, and the check shows that it is `unknown` since it was made. A check with a hold time (`for_seconds`) and only `unknown` results alerts once the hold time after it was made has passed, so an older check does not start its hold again after the upgrade.

## Action
None.
