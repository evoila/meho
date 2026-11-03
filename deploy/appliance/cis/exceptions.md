# CIS exceptions of the MEHO appliance

Rules of the CIS Ubuntu Linux 24.04 LTS Benchmark v1.0.0, Level 1 - Server,
that apply to the appliance but fail on the build's scan. Each has one
sentence on why. Damir Topic approves the whole list once per release, with
the release's security sheet (ADR 0009 decision 2); a release does not ship
without that approval.

The build's scan (`harden.sh`) warns about every failing rule that is not in
this table or in `not-applicable.md`. The scanner rule names are those of
ComplianceAsCode 0.1.82.

Status: a draft for the first build. The week-4 build fills it from a real
scan.

| Scanner rule | CIS | Why it fails |
| --- | --- | --- |
| `grub2_password` | 1.4.1 | Set at first start: each appliance makes its own bootloader password, so the build has none. A scan on an appliance after first start passes. |
| `aide_build_database` | 6.3.1 | Set at first start: each appliance builds the AIDE database of its own files, so the build has none. A scan on an appliance after first start passes. |
| `set_nftables_loopback_traffic` | 4.3.6 | The check wants `iif "lo" accept` and an `ip saddr` rule at the top of the input chain. The MEHO firewall file accepts `iifname "lo"` and drops nothing by source address there. A change to the form-2 firewall file is proposed. |
