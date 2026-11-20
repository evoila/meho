## Note
On a Linux server (install form 2), `meho-host certificate` also gives the CA of your certificate to MEHO (the ConfigMap `meho-console-trust`) and restarts MEHO, so the `certificate` line of the first-start list turns green; the console is away for about a minute. With the install's own CA the line stays red, as before. When the CA cannot be read, the line says the certificate is not trusted and names `sudo meho-host certificate`.

## Action
If you installed your own certificate with `meho-host certificate` before this version, run `sudo meho-host certificate --cert chain.pem --key key.pem` again with the same files after the update, so the `certificate` line turns green. See https://evoila.github.io/meho/latest/install/form-2-linux-server/#use-a-certificate-from-your-own-ca.
