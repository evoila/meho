## Note
The default answer of every operation also hides secret-named values in a YAML comment line (`# dbPassword: ...`, with a commented-out value over more lines), in `key: value` lines of a text that does not read as YAML, and in XML text: a secret-named element, a secret-named attribute, the `value` and the text of an element whose `key` or `name` is a secret name (`<add key="DbPassword" value="..."/>`), and the `<value>` of a `<name>`/`<value>` pair whose name is a secret name.

## Action
None. A script that reads such a text sees `[REDACTED:param_name]` in place of the value. A person who needs the value sends the call with `reveal_secret: true`.
