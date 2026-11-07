# Tools for your laptop

Operators and agents reach MEHO from their own machines.

- **The `meho` command:** [The meho CLI](../clients/cli.md) says how to
  download it, check it and log in.
- **Agents:** [Connect clients](../clients/index.md) covers Claude
  Desktop, Claude Code and other MCP clients.
- **A company certificate authority:** if your MEHO uses a certificate
  from an internal authority, install that authority in your laptop's
  system trust store first. See
  [TLS and ingress](tls-ingress.md#your-workstation-os-trust-store).

`meho login` saves the address of the backplane you log in to. A later
`meho login` to another address replaces it, and later commands go
to the new one. Run `meho status` after each login to see which
backplane you talk to.

Next: [back up, restore and update](backup-restore-update.md).
