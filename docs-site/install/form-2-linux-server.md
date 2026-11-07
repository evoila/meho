# Form 2: the Linux server

!!! note "Available, in testing"

    Use this form for tests and pilots. Talk to evoila before you run
    it in production.

One download holds everything: the install script, a small built-in
Kubernetes (RKE2), the MEHO chart, every container image and the `meho`
command. One script installs MEHO with its database, login server and
password safe.

The steps are on the page [Ways to install
MEHO](ways-to-install.md#the-linux-server-install). It says what you
need, how to check the download and how to run `install.sh`.

When the script ends, it prints the address of the console and the
two files with the first-login passwords and the unseal key. Keep both
files in your own key store (see [OpenBao unseal
key](openbao-unseal-key.md)). Then go on with
[the setup page and the settings file](setup-page.md).
