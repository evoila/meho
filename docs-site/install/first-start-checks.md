# The first-start checks

Check that MEHO is healthy before you use it.

1. **Wait for the first start.** On an empty database, the first start
   takes up to 15 minutes. A Pod that is `Running` but not `Ready` for
   some minutes is normal.
2. **Check the Pods and the health pages.** Step 5 of
   [the Kubernetes chapter](form-3-your-kubernetes.md#step-5-check-that-it-works)
   shows the commands. `/healthz` must answer, and `/ready` must show
   every check green.
3. **Check your own machine.** `meho doctor` names the layer that
   fails. It only reports and changes nothing. On a Linux server
   install, run `sudo meho doctor` on the server too. See
   [Troubleshooting](../clients/troubleshooting.md#start-with-meho-doctor).

Next: [your first day](first-day.md).
