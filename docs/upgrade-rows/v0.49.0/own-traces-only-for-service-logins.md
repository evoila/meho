## Note
An agent login and a service login now see only the stored call traces (flight recorder) of their own calls, on `GET /api/v1/audit/{audit_id}/trace` and `GET /api/v1/audit/traces`. This holds for a paired add-on, because it logs in as a service login. It also holds for a client-credentials login whose token has no `principal_kind` claim: MEHO treats such a login as a service login. People still see every trace of their tenant.

## Action
If a paired add-on or another client-credentials login must read the traces of calls that a person or another login made, give that work to a person login. A trace that such a login may not read answers `trace_present=false` on the single read, and is left out of the list on the search.
