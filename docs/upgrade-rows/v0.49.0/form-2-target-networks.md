## Note
On a Linux server (install form 2), you can now name the networks of the systems that MEHO manages, with `install.sh --target-networks <list>` or `sudo meho-host target-networks <list>`: IPv4 ranges, addresses and host names, each address or name with an optional port. MEHO accepts a target on a private address only inside them (the chart value `config.targetSsrfAllowlist`), and the update keeps the list. Without a list, MEHO refuses every target on a private address, as before.

## Action
On a Linux server, run `sudo meho-host target-networks <list>` after the update, with the networks of your vCenter, switches and other managed systems, for example `10.20.0.0/16,vcenter.example.com:443`. MEHO restarts for this. See https://evoila.github.io/meho/latest/install/form-2-linux-server/#the-target-networks.
