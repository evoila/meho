## Note
With the setting `CUSTOM_POLICY` on, `meho policy export` also writes the risk-level edits of the scope in the part `risk_levels`: each edit with its operation, level or approval flag, reason, who set it and when. A tenant's file holds only that tenant's edits; the install's file holds the install-wide edits. An import of the part as exported changes nothing ("No change."); an import of a changed part is refused with the line named, because this version does not change the edits from a file. With the setting off, the export has no such part and a file with it is refused as before. A roll-back to v0.48 is safe: nothing new is stored, and v0.48 refuses a file with `risk_levels`, so remove the part before you import an exported file there.

## Action
None.
