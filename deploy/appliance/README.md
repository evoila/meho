# The appliance build

Builds `meho-appliance-<version>.ova`, install form 1 of MEHO: a Linux
machine where the form-2 install already ran, cleaned and exported. The
design is the internal design note `form-1-appliance.md`, section 1.
The code walkthrough is `docs/codebase/appliance-build.md`.

| File | What |
| --- | --- |
| `build.sh` | The five steps against the lab's vCenter: base image, form-2 install, appliance pieces, clean, export. |
| `clean.sh` | Runs on the build VM: removes everything that must differ between appliances, checks itself, powers off. |
| `rewrite-ovf.sh` | Puts our product section and the OVF transports into the exported `.ovf`. |
| `product-section.xml` | The wizard fields of the import in vCenter. |
| `tests/` | Unit tests of `clean.sh` and `rewrite-ovf.sh`. No vCenter. |

`.github/workflows/appliance-build.yml` runs the build on the lab runner.

## Run it by hand

On a Linux machine that reaches the lab's vCenter and the build VM's
address, with `govc`, `ssh`, `scp`, `curl`, `tar` and `sha256sum`:

```bash
export GOVC_URL=https://vcenter.example.lab GOVC_USERNAME=... GOVC_PASSWORD=... GOVC_INSECURE=1
export GOVC_DATASTORE=... GOVC_FOLDER=meho-appliance GOVC_RESOURCE_POOL=... GOVC_NETWORK=...
export MEHO_BUILD_HOSTNAME=meho-build.example.lab
export MEHO_BUILD_ADDRESS=10.0.0.10/24 MEHO_BUILD_GATEWAY=10.0.0.1 MEHO_BUILD_DNS=10.0.0.2
ssh-keygen -t ed25519 -N '' -f /tmp/build-key
export MEHO_BUILD_SSH_KEY=/tmp/build-key
export MEHO_DOWNLOAD=/path/to/meho-linux-0.36.0-amd64.tar
deploy/appliance/build.sh 0.36.0 /tmp/ova
```

The form-2 download is the one of the release, checked as
`docs/RELEASING.md` says. `MEHO_BUILD_HOSTNAME` needs a DNS record in the
lab that points to `MEHO_BUILD_ADDRESS`, because the form-2 install checks
it. Make a new SSH key for every build.

When a step fails, the build VM stays in vCenter. Fix the cause and go on
from that step with `--from-step N` (2 the form-2 install, 3 the appliance
pieces, 4 the clean, 5 the export), or destroy the VM with
`govc vm.destroy <name>`.

A failed clean is different. `--from-step 4` works only when `clean.sh`
stopped in its step 1 (the Secrets), while RKE2 still runs and SSH is
open. After any later failure, RKE2 is stopped, and the build user and SSH
may be gone: read the `[FAIL]` and `[LEFT]` lines on the VM console in
vCenter, destroy the VM with `govc vm.destroy <name>`, and build again
from step 1.

## Test it

The unit tests need Linux bash 5 (macOS bash 3.2 cannot run the scripts):

```bash
docker run --rm -v "$PWD/deploy/appliance:/work/deploy/appliance:ro" -w /work python:3.14-slim python -m unittest discover -s deploy/appliance/tests -v
```

```bash
shellcheck deploy/appliance/*.sh
```
