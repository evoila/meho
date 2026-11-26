## Note
The policy file reserves seven top-level keys: `limits`, `agents`, `approvers`, `role_bindings`, `risk_levels`, `custom_roles` and `baseline`. A file that holds one of them is refused, whatever its value. The answer is one sentence, for example "This version of MEHO does not read `agents` in a policy file; remove it." So no stored policy holds them, and v0.48 reads every stored policy after a roll-back (it also skips a key it does not know).

## Action
None.
