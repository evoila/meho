## Note
The default answer of every operation also hides secret-named values in a YAML comment line (`# dbPassword: ...`), in `key: value` lines of a text that does not read as YAML, and in XML text: a secret-named element, a secret-named attribute, and the `value` of a `<add key="DbPassword" value="..."/>` pair.

## Action
None. A script that reads such a text sees `[REDACTED:param_name]` in place of the value. A person who needs the value sends the call with `reveal_secret: true`.
