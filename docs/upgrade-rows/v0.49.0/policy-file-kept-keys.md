## Note
The policy file reserves seven top-level keys: `limits`, `agents`, `approvers`, `role_bindings`, `risk_levels`, `custom_roles` and `baseline`. A file that holds `agents`, `approvers`, `role_bindings`, `risk_levels`, `custom_roles` or `baseline` is refused, whatever its value, with one sentence, for example "This version of MEHO does not read `agents` in a policy file; remove it." A tenant's file that holds `limits` is refused. The install's file may hold `limits` only while the setting `CUSTOM_POLICY` is on; with it off, the file is refused with "`limits` needs CUSTOM_POLICY: ...", and with it on, the install's stored policy can hold a `limits` part. v0.48 reads every stored policy after a roll-back (it skips a key it does not know); what a roll-back does with a stored `limits` part is in the row install-policy-outer-limit.

## Action
None.
