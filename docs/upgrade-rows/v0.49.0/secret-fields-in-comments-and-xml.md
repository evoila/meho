## Note
The default answer of every operation also hides a secret-named value in a YAML comment (`# dbPassword: ...`), in a `key: value` line of a text that is not valid YAML, and in an XML element or attribute (`<dbPassword>...</dbPassword>`, `dbPassword="..."`) inside a text value.

## Action
None. A script that reads such a text sees `[REDACTED:param_name]` in place of the value. A person who needs the value sends the call with `reveal_secret: true`.
