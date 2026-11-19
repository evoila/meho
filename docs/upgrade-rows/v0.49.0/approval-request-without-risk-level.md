## Note
A waiting approval request with no stored risk level now counts as `destructive`: only a MEHO-SA-Operator or a MEHO-Admin may approve it, and the self-approval emergency switch does not cover it. New requests keep the risk level of their operation also when MEHO could not build their preview, and the approval card then says "MEHO could not build the preview of this change."

## Action
None for most installs. If a request that waits from before the upgrade has no risk level on its card, ask a MEHO-SA-Operator or a MEHO-Admin to approve it, or reject it and run the call again.
