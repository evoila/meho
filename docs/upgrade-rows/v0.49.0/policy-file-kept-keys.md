## Note
The policy file knows five new top-level keys: `limits`, `agents`, `approvers`, `role_bindings` and `risk_levels`. A file with one of them is still refused: `limits` needs the `CUSTOM_POLICY` switch, and the other four say "not in this version". So no stored policy holds them, and v0.48 reads every stored policy as before after a roll-back (it also skips a key it does not know).

## Action
None.
