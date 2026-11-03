# CIS rules that do not apply to the MEHO appliance

The appliance runs Kubernetes (RKE2) for MEHO. A few rules of the CIS Ubuntu
Linux 24.04 LTS Benchmark v1.0.0, Level 1 - Server, cannot apply to such a
machine. Each one is here with its reason, in our own words (ADR 0009
decision 2). The release's security sheet lists them.

The automated rules below are left out of the scan in `tailoring.xml`. A test
(`tests/test_cis_lists.py`) checks that this table and the tailoring name the
same rules. The scanner rule names are those of ComplianceAsCode 0.1.82.

| Scanner rule | CIS | Reason |
| --- | --- | --- |
| `sysctl_net_ipv4_ip_forward` | 3.3.1 | IP forwarding stays on: RKE2 passes traffic to its own pods. The host firewall still drops all other traffic through the machine. |
| `package_ufw_installed` | 4.2.1 | CIS offers three firewall choices (4.1.1). The appliance uses nftables (4.3), so the ufw section does not apply. |
| `service_ufw_enabled` | 4.2.3 | The ufw section does not apply: the appliance uses nftables (4.1.1). |
| `check_ufw_active` | 4.2.3 | The ufw section does not apply: the appliance uses nftables (4.1.1). |
| `set_ufw_loopback_traffic` | 4.2.4 | The ufw section does not apply: the appliance uses nftables (4.1.1). |
| `ufw_rules_for_open_ports` | 4.2.6 | The ufw section does not apply: the appliance uses nftables (4.1.1). |
| `set_ufw_default_rule` | 4.2.7 | The ufw section does not apply: the appliance uses nftables (4.1.1). |
| `package_iptables-persistent_installed` | 4.4.1.1 | CIS offers three firewall choices (4.1.1). The appliance uses nftables (4.3), so the iptables section does not apply. |
| `package_iptables_installed` | 4.4.1.1 | The iptables section does not apply: the appliance uses nftables (4.1.1). |
| `service_nftables_disabled` | 4.4.1.2 | The iptables section does not apply: the appliance uses nftables (4.1.1), so `nftables.service` is on (4.3.9). |
| `set_iptables_default_rule` | 4.4.2.1 | The iptables section does not apply: the appliance uses nftables (4.1.1). |
| `set_loopback_traffic` | 4.4.2.2 | The iptables section does not apply: the appliance uses nftables (4.1.1). |
| `iptables_rules_for_open_ports` | 4.4.2.4 | The iptables section does not apply: the appliance uses nftables (4.1.1). |
| `set_ip6tables_default_rule` | 4.4.3.1 | The iptables section does not apply: the appliance uses nftables (4.1.1). |
| `set_ipv6_loopback_traffic` | 4.4.3.2 | The iptables section does not apply: the appliance uses nftables (4.1.1). |
| `ip6tables_rules_for_open_ports` | 4.4.3.4 | The iptables section does not apply: the appliance uses nftables (4.1.1). |
| `no_files_unowned_by_user` | 7.1.12 | Files in container layers and volumes belong to user ids that have no account on the host. The rule covers the host's own files. |
| `file_permissions_ungroupowned` | 7.1.12 | Files in container layers and volumes belong to group ids that have no group on the host. |

## Manual rules

These CIS rules have no automated check, so the scan does not list them. They
do not apply, for these reasons:

| CIS | Reason |
| --- | --- |
| 2.1.22 | RKE2's own ports listen on the network card. The host firewall blocks them from other machines; only 80, 443 and SSH from the admin network answer. |
| 4.3.3 | Canal and kube-proxy write iptables rules (through nftables) that must stay. The MEHO firewall never flushes them. |
| 7.1.13 | Container layers hold SUID and SGID files. The review covers the host's own paths. |
