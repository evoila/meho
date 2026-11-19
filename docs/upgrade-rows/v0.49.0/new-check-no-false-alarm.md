## Note
Adding a check to a dashboard that already has a state no longer sends a false alarm. MEHO waits for the new check's first confirmed result, but not forever. If the check has no confirmed result by its first due time, plus the time its result confirmation takes, plus 60 seconds, MEHO alerts as `degraded`. It also alerts when the check is more than 60 seconds overdue. A check that already had a confirmed state does not wait.

## Action
None.
