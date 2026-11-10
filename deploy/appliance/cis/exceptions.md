# CIS exceptions of the MEHO appliance

Rules of the CIS Ubuntu Linux 24.04 LTS Benchmark v1.0.0, Level 1 - Server,
that apply to the appliance but fail on the build's scan or on an appliance
after first start. Each has one sentence on why. Damir Topic approves the
whole list once per release, with the release's security sheet (ADR 0009
decision 2); a release does not ship without that approval
(`docs/RELEASING.md`, pre-flight).

The build's scan (`harden.sh`) stops the build when a rule fails that is in
neither this table nor `not-applicable.md`. The scanner rule names are
those of ComplianceAsCode 0.1.82.

Status: filled from the scans of the first lab build (week 4, 2026-11-04):
one at the end of the build, one on an appliance after first start.

| Scanner rule | CIS | Fails on | Why it fails |
| --- | --- | --- | --- |
| `grub2_password` | 1.4.1 | the build | Set at first start: each appliance makes its own bootloader password, so the build has none. It passes on an appliance after first start. |
| `grub2_uefi_password` | 1.4.1 | the build | The same rule as `grub2_password`, for a machine that boots with UEFI. It passes on an appliance after first start. |
| `aide_build_database` | 6.3.1 | the build and the appliance | First start builds the AIDE database of each appliance (`/var/lib/aide/aide.db`). The check still fails, because it looks for a `database=` line in `aide.conf`, and AIDE 0.18 on Ubuntu 24.04 calls that line `database_in=`. |
| `file_permissions_unauthorized_world_writable` | 7.1.11 | the appliance | The world-writable files are not files of the host's own system: the kubelet makes a termination-message file with mode `0666` for each container, and one more is inside a container image. |
