## Note
A password or token on a command line is now hidden. Command lines in operation results, audit rows and traces show `[REDACTED:command_line_secret]` in place of the password (for example `mysql -p…`, `sshpass -p …`, `--password …`, `curl -u user:…`, `sqlplus user/…@db` and `Password=…;` in a connection string). The rest of the command line stays readable. A command line with no password is shown as before.

## Action
None for most operators. A script or a check that reads a password from a command line in a result now gets the hidden mark: read the password from Vault or the target's credentials instead.
