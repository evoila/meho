## Note
A webhook event now starts only an `event` trigger whose filter names the event source, by slug (`{"source": {"slug": "<source-slug>"}}`) or by id. Only deliveries that pass the sender check count against an event source's rate limit; failed checks have their own limit of the same size. An event source secret that is empty or only spaces is refused with 422, and a source whose stored secret is empty accepts no sender. No schema migration.

## Action
For each `event` trigger that should start on webhook events, add `"source": {"slug": "<source-slug>"}` to its filter. Without it, the trigger does not start on webhook events. See [Event ingestion](https://evoila.github.io/meho/latest/guides/event-ingestion/).
