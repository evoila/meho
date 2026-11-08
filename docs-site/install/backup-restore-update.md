# Back up, restore and update

- **Update:** [Upgrades](upgrades.md) says what `helm upgrade` does,
  how to roll back, and what changes in each version. On a Linux
  server, run `install.sh` from the newer download on the same machine
  (see [the Linux server chapter](form-2-linux-server.md)).
- **Back up and restore:** [Back up and restore the knowledge
  store](upgrades.md#back-up-and-restore-the-knowledge-store) shows how
  to dump and restore the database.
- **The settings file** is not a full backup. A full restore also needs
  a backup of the password safe and of the Kubernetes Secrets the
  install made. See [the settings file](../guides/settings-file.md).

Take a snapshot or a backup before each update.

Next: [common problems](common-problems.md).
