# Scenario 08: Network Configuration Analysis

**Category:** Inventory / Health Check  
**Complexity:** Intermediate  
**Connector:** VMware pyvmomi

## Goal

Analyze network configuration including distributed switches, port groups,
VLANs, and VM network assignments.

## Context

Network configuration in vSphere involves distributed virtual switches (DVS),
port groups, and VLAN assignments. Operators need to verify network configuration
for troubleshooting connectivity issues or planning network changes.

---

## User Queries

### Query 1: List all networks

```
Show me all networks in the vCenter
```

**Expected MEHO Behavior:**
1. Recognize intent: FETCH
2. Find operation: `list_networks`
3. Execute and return network list

**Operation Used:**
```
list_networks()
```

**Returns per network:**
- name
- type (Network, DistributedVirtualPortgroup)
- accessible
- vlan_id (if applicable)

### Query 2: List distributed switches

```
List all distributed virtual switches
```

**Expected MEHO Behavior:**
1. Find operation: `list_distributed_switches`
2. Execute and return DVS list

**Operation Used:**
```
list_distributed_switches()
```

**Returns per DVS:**
- name
- uuid
- version
- num_ports
- num_hosts (connected hosts)
- config_version

### Query 3: DVS details

```
Show me the configuration of switch DVS-Production
```

**Expected MEHO Behavior:**
1. Find operation: `get_distributed_switch`
2. Execute with DVS name
3. Return detailed configuration

**Operation Used:**
```
get_distributed_switch(dvs_name="DVS-Production")
```

**Returns:**
- All fields from list plus:
- hosts (connected ESXi hosts)
- portgroups (list of port groups on this DVS)
- uplinks configuration
- default port configuration

### Query 4: List port groups

```
What port groups are on switch DVS-Production?
```

**Expected MEHO Behavior:**
1. Option A: Use `list_port_groups` and filter by DVS
2. Option B: Get from DVS details above
3. Return port groups with VLAN info

**Operation Used:**
```
list_port_groups()
```

**Returns per port group:**
- name
- type (early binding, ephemeral)
- vlan_id
- dvs_name (parent switch)
- num_ports

### Query 5: Port group details with VLAN

```
What VLAN is configured for port group VLAN-100?
```

**Expected MEHO Behavior:**
1. Find operation: `get_port_group`
2. Execute with port group name
3. Return VLAN configuration

**Operation Used:**
```
get_port_group(portgroup_name="VLAN-100")
```

**Returns:**
- name
- vlan_id
- vlan_type (none, vlan, trunk, pvlan)
- dvs_name
- security_policy (promiscuous, mac_changes, forged_transmits)
- teaming_policy

### Query 6: VMs on a VLAN

```
Which VMs are on VLAN 100?
```

**Expected MEHO Behavior:**
1. Find networks/port groups with VLAN 100
2. List VMs connected to those networks
3. May need to cross-reference VM network adapters

**Steps:**
1. `list_port_groups()` - find VLAN 100 port groups
2. For each VM, check network assignments
3. Or filter VM list by network name

### Query 7: VM network adapter details

```
Show me the network adapters for VM web-server-01
```

**Expected MEHO Behavior:**
1. Find operation: `get_vm_nics`
2. Execute with VM name
3. Return adapter details

**Operation Used:**
```
get_vm_nics(vm_name="web-server-01")
```

**Returns per adapter:**
- label (e.g., "Network adapter 1")
- type (vmxnet3, e1000, e1000e)
- mac_address
- network_name
- connected (true/false)
- start_connected
- port_key (for DVS)

### Query 8: VLANs in use on a DVS

```
What VLANs are configured on DVS-Production?
```

**Expected MEHO Behavior:**
1. Find operation: `query_used_vlans`
2. Execute with DVS name
3. Return list of VLAN IDs

**Operation Used:**
```
query_used_vlans(dvs_name="DVS-Production")
```

**Returns:**
- List of VLAN IDs in use on the DVS

### Query 9: Change VM network (ACTION)

```
Move web-server-01 network adapter 1 to VLAN-200 network
```

**Expected MEHO Behavior:**
1. Recognize intent: ACTION
2. Find operation: `change_network`
3. Request approval
4. Execute after approval

**Operation Used (after approval):**
```
change_network(
    vm_name="web-server-01",
    adapter_label="Network adapter 1",
    network_name="VLAN-200"
)
```

---

## Network Architecture Example

```
DVS-Production
├── VLAN-100 (Production)
│   ├── web-server-01 (nic1)
│   ├── web-server-02 (nic1)
│   └── app-server-01 (nic1)
├── VLAN-200 (Database)
│   ├── db-primary (nic1)
│   └── db-replica (nic1)
├── VLAN-300 (Management)
│   ├── all servers (nic2)
│   └── ...
└── VLAN-999 (Trunk - all VLANs)
    └── firewall-01 (nic1)
```

---

## Operations Reference

| Operation | Description | Parameters | Danger |
|-----------|-------------|------------|--------|
| `list_networks` | All networks | None | Safe |
| `list_distributed_switches` | All DVS | None | Safe |
| `get_distributed_switch` | DVS details | `dvs_name` | Safe |
| `list_port_groups` | All port groups | None | Safe |
| `get_port_group` | Port group details | `portgroup_name` | Safe |
| `get_vm_nics` | VM network adapters | `vm_name` | Safe |
| `get_host_networks` | Networks on a host | `host_name` | Safe |
| `query_used_vlans` | VLANs in use on DVS | `dvs_name` | Safe |
| `change_network` | Change VM network | `vm_name`, `adapter_label`, `network_name` | MEDIUM |
| `add_network_adapter` | Add NIC to VM | `vm_name`, `network_name`, `adapter_type` | MEDIUM |
| `remove_network_adapter` | Remove NIC from VM | `vm_name`, `adapter_label` | MEDIUM |

---

## Use Cases

1. **Connectivity Troubleshooting**
   - "Why can't VM X reach VLAN Y?"
   - Check: Is VM on correct port group? Is VLAN configured?

2. **Migration Planning**
   - "Which VMs need network changes before migration?"
   - List VMs on networks not available at target

3. **Compliance Verification**
   - "Are all production VMs on the correct networks?"
   - Cross-reference VM tags with network assignments

4. **Network Cleanup**
   - "Which port groups have no VMs connected?"
   - Find unused port groups for cleanup

---

## Success Criteria

- [ ] Networks listed with VLAN info via `list_networks`
- [ ] Distributed switches queryable via `list_distributed_switches`
- [ ] DVS details include port groups via `get_distributed_switch`
- [ ] Port groups filterable by DVS
- [ ] VLAN configuration visible via `get_port_group`
- [ ] VMs queryable by network/VLAN
- [ ] VM adapter details retrievable via `get_vm_nics`
- [ ] Network change triggers approval
- [ ] VLANs in use queryable via `query_used_vlans`
