## Note
Adding a check to a dashboard that already has a state no longer sends a false alarm. MEHO waits for the new check's first result. If the check never runs, MEHO alerts as `degraded` once it is more than 60 seconds overdue.

## Action
None.
