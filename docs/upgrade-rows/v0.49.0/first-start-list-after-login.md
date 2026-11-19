## Note
The first-start list needs a login with the operator role or higher. `meho status --first-start` reads it from the new route `GET /api/v1/first-start` with the login of `meho login`. It checks the systems of the current tenant; for a platform admin, the systems of all tenants. `GET /first-start` without a login answers one fixed line that says how to sign in. It runs no check. At the end of `install.sh` (Linux server), the result names where to read the list after the first login.

## Action
Read the first-start list after you sign in: open `/ui/first-start` in the web console, or run `meho login <address>` and then `meho status --first-start`. A script that reads `GET /first-start` without a login gets the fixed line: give it a login and call `GET /api/v1/first-start`.
