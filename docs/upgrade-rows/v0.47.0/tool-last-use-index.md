## Note
Migration `0173` adds one more index on `audit_log` (person, login client and time). With it, "Your tools" on the account page shows the exact last use of each tool, also for a person with many audit rows. It changes no row. While the index is built, calls that write an audit row wait: about 7 seconds for 3.3 million rows in a test, longer on a larger audit log.

## Action
**Nothing to do.** The migration runs by itself. With a large audit log, upgrade at a quiet time.
