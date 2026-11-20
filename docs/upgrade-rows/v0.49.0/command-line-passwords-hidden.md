## Note
A password or token on a command line is now hidden. Command lines in operation results, traces, broadcast messages and error texts show `[REDACTED:command_line_secret]` in place of the password (for example `mysql -p…`, `sshpass -p …`, `--password …`, `curl -u user:…`, `sqlplus user/…@db` and `Password=…;` in a connection string). The rest of the command line stays readable. A quoted password may run over line breaks, but a closing quote more than 64 lines further down does not count, so one quote that is never closed does not hide the log lines after it. A command line with no password is shown as before. The raw copy in an audit row is not changed by this rule.

## Action
None for most operators. A script or a check that reads a password from a command line in a result now gets the hidden mark: read the password from Vault or the target's credentials instead.
