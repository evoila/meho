# OpenBao unseal key: who keeps what

MEHO's built-in password safe, OpenBao, locks itself ("seals") every time
it restarts. To open ("unseal") it again, it needs its **unseal key**. This
page says where that key is kept, how to choose who unseals OpenBao, and
what to do in an emergency.

!!! note "This page is a first version"
    The sections on who keeps the key and on backup and restore are not
    written yet.

The commands below are for an install on your own Kubernetes. Replace
`<namespace>` and `<release>` with your Helm namespace and release name.
Where MEHO's installer runs the cluster for you, the installer has its own
commands for the same steps.

## Two modes

| Mode | When | What happens after a restart |
| --- | --- | --- |
| **Self-unseal** (the default) | The Secret `<release>-core-vault-unseal` exists | A small helper container in the OpenBao pod reads the key from the Secret and unseals OpenBao within seconds. No person is needed. |
| **Keyholder at boot** | The Secret does not exist | OpenBao stays sealed. MEHO is not ready (`/ready` says `vault: sealed`) until a keyholder unseals it. |

There is no setting for the mode. The mode is only whether the Secret
exists. A restart happens more often than you may think: node maintenance,
a node reboot, and an upgrade that changes OpenBao's pod.

## What self-unseal protects, and what it does not

In self-unseal mode, the unseal key is stored next to the data it opens.

- It does **not** protect against anyone who can read Secrets or create pods
  in the namespace, anyone with root on the node, or anyone who copies the
  whole disk.
- It protects only against a copy of OpenBao's disk or a snapshot taken
  **without** the Secret.
- Kubernetes keeps Secrets in its own database. On k3s they are plain text
  unless you turn on encryption. RKE2 encrypts them, but keeps the
  encryption key on the same disk.

So the real protection is the namespace and the disk: limit who can read
Secrets in the namespace, and encrypt the node disks. If your security team
wants more, switch to keyholder at boot. The price: a person must unseal
OpenBao after every restart, and MEHO waits until then.

## Save the key now

Right after the install, copy the key to your key store (for example your
password manager or a safe):

```bash
kubectl -n <namespace> get secret <release>-core-vault-unseal \
  -o jsonpath='{.data.key}' | base64 -d
```

The install notes print this command. The chart never shows the key itself,
and no page in MEHO shows it. There is no root token after the install.

## Switch the mode

### From self-unseal to keyholder at boot

1. Copy the key to your key store (above), and check the copy.
2. Delete the Secret and restart OpenBao:

    ```bash
    kubectl -n <namespace> delete secret <release>-core-vault-unseal
    kubectl -n <namespace> rollout restart statefulset <release>-vault
    ```

After the restart, OpenBao stays sealed until a keyholder unseals it.

### From keyholder at boot to self-unseal

1. Create the Secret again from your key store. These commands keep the key
   out of your shell history. Spaces or a line break around the key do no
   harm: the helper removes them before it unseals.

    ```bash
    read -rs KEY    # paste the key, press Enter; it is not shown
    printf %s "$KEY" | kubectl -n <namespace> create secret generic \
      <release>-core-vault-unseal --from-file=key=/dev/stdin
    unset KEY
    ```

2. Restart OpenBao:

    ```bash
    kubectl -n <namespace> rollout restart statefulset <release>-vault
    ```

### Unseal by hand (keyholder at boot)

```bash
kubectl -n <namespace> exec -it <release>-vault-0 -c bao -- bao operator unseal
```

It asks for the key and does not show it. Do **not** create the Secret again
just to unseal once: that switches the mode back to self-unseal.

## More than one keyholder

The install makes one key. To split it among several people (for example
three people, any two of them needed):

1. Switch to keyholder at boot (above). OpenBao must be unsealed for the
   next step.
2. With an admin login, start a key rotation with
   `bao operator rotate-keys -init -key-shares=3 -key-threshold=2`, then
   give the current key. Each person gets one new key.

After that, self-unseal no longer works: the helper has one key, and
OpenBao needs two. To go back, rotate to one key (`-key-shares=1
-key-threshold=1`). See the OpenBao docs:
[rotate-keys](https://openbao.org/docs/commands/operator/rotate-keys/).

## Emergency: recovery mode

Use this only when OpenBao's data is damaged or nobody can log in any more,
and you still have the unseal key.

- Start the OpenBao server with the `-recovery` flag. There is no normal
  unseal in this mode: make a recovery token from the unseal key instead
  (`bao operator generate-root -recovery-token`, in four steps).
- With the recovery token you can read and repair OpenBao's raw storage
  (`sys/raw`). No root token and no admin login are needed.
- The other ways to make a new root token without logging in stay switched
  off, on purpose.

See the OpenBao docs:
[recovery mode](https://openbao.org/docs/concepts/recovery-mode/).

<!-- To be written: the exact steps to start the OpenBao pod in recovery mode. -->

## Who keeps the key

<!-- To be written: the rule for who keeps the key, where, and how it is handed over. -->

This section is not written yet.

## Backup and restore

<!-- To be written: what the backup holds and the restore steps. -->

This section is not written yet. What is already decided:

- A backup of OpenBao opens only with the unseal key that was in use when
  the backup was made. Keep the key with the backup.
- In self-unseal mode, the backup must include the Secret
  `<release>-core-vault-unseal`. In keyholder mode, the keyholders keep the
  key.
- To restore, create the Secret first, then restore the data.
