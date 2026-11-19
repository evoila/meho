## Note
A waiting approval request with no stored risk level now counts as `destructive`: only a person who may approve another person's `destructive` change may approve it, the self-approval emergency switch does not cover it, and its approval sign-in can approve only this one request. New requests keep the risk level of their operation also when MEHO could not build their preview, and the approval card then says "MEHO could not build the preview of this change."

## Action
None for most installs. If a request that waits from before the upgrade has no risk level on its card, ask a person who may approve another person's `destructive` change to approve it, or reject it and run the call again.
