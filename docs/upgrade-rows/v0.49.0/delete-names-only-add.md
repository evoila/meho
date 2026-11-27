## Note
With the switch `CUSTOM_POLICY` on, the setting `SERVICE_GRANT_DELETE_SHAPED_PATTERNS` only adds names to the built-in delete names. On MEHO versions without this change, a set value is the whole list. So an install whose setting leaves out a built-in name gets that name back when it turns the switch on. Then more calls by a service login wait for a person: a standing grant does not cover an operation with a built-in delete name. With the switch off, nothing changes.

## Action
None. If you turn on `CUSTOM_POLICY`, keep only your own extra delete names in `SERVICE_GRANT_DELETE_SHAPED_PATTERNS`.
