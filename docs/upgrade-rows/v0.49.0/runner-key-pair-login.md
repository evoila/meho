## Note
A runner can now log in with a key pair it makes at its own site, and renews its own short token. `meho runner-principal register` prints a one-time enrollment code. The change adds a database migration and the route `/api/v1/runner-principals/enroll`. A runner with the fixed `MEHO_RUNNER_TOKEN` works as before.

## Action
Nothing is needed to upgrade. To use the key login, start the runner with `MEHO_RUNNER_ENROLLMENT_CODE` set to the code from `meho runner-principal register`, and keep `MEHO_RUNNER_KEY_DIR` on a volume that stays. See https://evoila.github.io/meho/latest/guides/satellite-gateway/ for the steps.
