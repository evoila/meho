# Scenario 10: Multi-Step VM Performance Diagnostic

**Category:** Monitoring / Diagnostics  
**Complexity:** Advanced (Multi-step reasoning with PerformanceManager API)  
**Connector:** VMware pyvmomi

## Goal

Diagnose a slow VM by gathering performance data, checking host resources,
verifying storage performance, and analyzing network configuration.

## Context

When a VM is reported as "slow," operators need to systematically check
multiple areas: CPU/memory contention, storage I/O, network latency, and
resource pool settings. This scenario tests MEHO's ability to conduct
a multi-step investigation using the detailed PerformanceManager API.

---

## User Queries (Conversation Flow)

### Turn 1: Initial complaint

```
VM web-server-01 is running slow, help me diagnose it
```

**Expected MEHO Behavior:**
1. Acknowledge the issue
2. Start gathering VM details
3. Begin systematic investigation

**Operation Used:**
```
get_virtual_machine(vm_name="web-server-01")
```

**Initial Response:**
```
I'll help diagnose the performance issue with web-server-01. Let me gather some information.

VM Details:
- Power State: poweredOn
- Host: esxi-03.example.com
- Cluster: PROD-Web-Cluster
- Datastore: DS-PROD-01
- Resource Pool: Production
- CPU: 4 vCPUs
- Memory: 16 GB
- Guest Tools: Running

Let me check the performance metrics...
```

### Turn 2: VM resource check

```
What are the current CPU and memory stats for that VM?
```

**Expected MEHO Behavior:**
1. Get detailed VM performance using PerformanceManager API
2. Include disk I/O and network throughput (not available in quickStats)
3. Return metrics with diagnostic highlights

**Operation Used:**
```
get_detailed_vm_performance(vm_name="web-server-01", interval="realtime")
```

**Returns:**
- CPU: usage_mhz, ready_percent (contention indicator), wait_time
- Memory: usage_mb, active_mb, ballooned_mb, swapped_mb
- Disk: read_kbps, write_kbps, read_iops, write_iops, latency_ms
- Network: receive_kbps, transmit_kbps, packets_dropped
- Diagnostic highlights (pre-analyzed issues)

**Expected Response:**
```
📊 Performance Metrics for web-server-01 (realtime)

CPU:
- Usage: 3,200 MHz / 4,000 MHz (80%)
- CPU Ready: 12% ⚠️ HIGH (indicates contention)
- Wait Time: 5%

Memory:
- Usage: 14,336 MB / 16,384 MB (87.5%)
- Active: 12,288 MB
- Ballooned: 0 MB ✓
- Swapped: 0 MB ✓

Disk I/O:
- Read: 45 MB/s (450 IOPS)
- Write: 12 MB/s (120 IOPS)
- Latency: 8 ms ✓

Network:
- Receive: 25 MB/s
- Transmit: 15 MB/s
- Dropped Packets: 0 ✓

⚠️ ISSUE DETECTED: CPU Ready time is 12% (threshold: 5%)
This indicates the VM is waiting for physical CPU time.
The host may be overloaded.
```

### Turn 3: Host resource check

```
Is the host it's running on overloaded?
```

**Expected MEHO Behavior:**
1. Identify the host from VM data (cached from Turn 1)
2. Get host performance metrics
3. Check if host is the bottleneck

**Operations Used:**
```
get_host(host_name="esxi-03.example.com")
get_detailed_host_performance(host_name="esxi-03.example.com", interval="realtime")
```

**Expected Response:**
```
📊 Host Performance: esxi-03.example.com

Hardware:
- CPU: 2x Intel Xeon, 24 cores total, 48,000 MHz capacity
- Memory: 256 GB

Current Usage:
- CPU: 42,000 MHz / 48,000 MHz (87.5%) ⚠️ HIGH
- Memory: 180 GB / 256 GB (70%)
- VMs on host: 18 VMs

Storage I/O (all VMs):
- Total Disk Throughput: 850 MB/s
- Average Latency: 6 ms ✓

Network:
- Total Network: 200 MB/s
- Physical NICs: 4x 10 Gbps

⚠️ HOST CPU IS HEAVILY LOADED (87.5%)
This explains the high CPU Ready time on your VM.
Other VMs on this host are likely also affected.
```

### Turn 4: Storage check

```
What about the datastore? Is there I/O contention?
```

**Expected MEHO Behavior:**
1. Identify datastores from VM data
2. Get datastore performance metrics
3. Check capacity and I/O latency

**Operations Used:**
```
get_datastore(datastore_name="DS-PROD-01")
get_detailed_datastore_performance(datastore_name="DS-PROD-01", interval="realtime")
```

**Expected Response:**
```
📊 Datastore: DS-PROD-01

Capacity:
- Total: 10.0 TB
- Free: 2.5 TB (25%)
- Utilization: 75%

Performance:
- Read Throughput: 450 MB/s
- Write Throughput: 180 MB/s
- Read IOPS: 15,000
- Write IOPS: 6,000
- Read Latency: 3 ms ✓
- Write Latency: 5 ms ✓

VMs on Datastore: 45

✓ Storage performance looks healthy
  Latency is within normal range (<10ms)
  Capacity is adequate (25% free)
```

### Turn 5: Network check

```
Check the network configuration too
```

**Expected MEHO Behavior:**
1. Get VM network adapters
2. Verify network connectivity and configuration

**Operation Used:**
```
get_vm_nics(vm_name="web-server-01")
```

**Expected Response:**
```
📊 Network Configuration: web-server-01

Network Adapter 1:
- Type: VMXNET3
- Network: VLAN-100-Production
- MAC: 00:50:56:ab:cd:ef
- Connected: Yes ✓
- IP: 10.0.100.25

Network Stats (from earlier):
- Receive: 25 MB/s
- Transmit: 15 MB/s
- Dropped Packets: 0 ✓

✓ Network configuration looks correct
  Connected to production VLAN
  No dropped packets detected
```

### Turn 6: Summary and recommendation

```
Give me a summary of what you found
```

**Expected MEHO Behavior:**
1. Synthesize all gathered information
2. Identify root cause
3. Provide actionable recommendations

**Expected Response:**
```
🔍 DIAGNOSTIC SUMMARY: web-server-01

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 ROOT CAUSE IDENTIFIED: Host CPU Contention

The VM is experiencing high CPU Ready time (12%) because the host
(esxi-03.example.com) is heavily loaded at 87.5% CPU utilization
with 18 VMs competing for resources.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FINDINGS:

| Area | Status | Details |
|------|--------|---------|
| VM CPU | ⚠️ Issue | 80% usage, 12% ready time |
| VM Memory | ✓ OK | 87.5% usage, no balloon/swap |
| Host CPU | ⚠️ Issue | 87.5% - overloaded |
| Host Memory | ✓ OK | 70% usage |
| Storage | ✓ OK | 5ms latency, 25% free |
| Network | ✓ OK | Connected, no drops |

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 RECOMMENDATIONS:

1. IMMEDIATE: Check DRS recommendations for this cluster
   → Command: "Show DRS recommendations for PROD-Web-Cluster"
   → DRS may suggest migrating VMs to balance load

2. SHORT-TERM: Consider vMotion to a less loaded host
   → esxi-01 is at 45% CPU - good target
   → Command: "Migrate VM web-server-01 to esxi-01"

3. LONG-TERM: Review resource pool CPU shares
   → Ensure critical VMs have higher priority
   → Check if Production pool has adequate reservations

4. MONITORING: Set up alerts for CPU Ready > 5%
   → This will catch contention issues earlier

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Would you like me to check DRS recommendations or initiate a vMotion?
```

---

## Complete Investigation Flow

```
┌────────────────────────────────────────────────────────────────┐
│  "VM web-server-01 is slow"                                    │
└────────────────────────────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────────────────┐
│  Step 1: Get VM Details                                        │
│  ├─ get_virtual_machine(vm_name="web-server-01")              │
│  └─ Cache: host, cluster, datastore, resource_pool             │
└────────────────────────────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────────────────┐
│  Step 2: Check VM Performance (PerformanceManager API)         │
│  ├─ get_detailed_vm_performance(..., interval="realtime")      │
│  ├─ CPU Usage: 80%                                             │
│  ├─ CPU Ready: 12% ⚠️ HIGH                                     │
│  ├─ Disk Latency: 8 ms ✓                                       │
│  └─ Network Drops: 0 ✓                                         │
└────────────────────────────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────────────────┐
│  Step 3: Check Host Resources                                  │
│  ├─ get_host(host_name=<from step 1>)                         │
│  ├─ get_detailed_host_performance(...)                         │
│  ├─ Host CPU: 87.5% ⚠️                                         │
│  └─ 18 VMs competing for resources                             │
└────────────────────────────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────────────────┐
│  Step 4: Check Storage                                         │
│  ├─ get_datastore(datastore_name=<from step 1>)               │
│  ├─ get_detailed_datastore_performance(...)                    │
│  └─ Storage: 5ms latency ✓                                     │
└────────────────────────────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────────────────┐
│  Step 5: Analysis                                              │
│  Finding: CPU contention at host level                         │
│  ├─ VM CPU is high but manageable                              │
│  ├─ CPU ready time elevated (12% vs 5% threshold)              │
│  └─ Root cause: Host overloaded (87.5%)                        │
└────────────────────────────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────────────────┐
│  Recommendations:                                              │
│  1. Check DRS recommendations                                  │
│  2. vMotion to less loaded host                               │
│  3. Review resource pool shares                                │
│  4. Set up CPU Ready alerts                                    │
└────────────────────────────────────────────────────────────────┘
```

---

## Key Metrics Thresholds

| Metric | Normal | Warning | Critical |
|--------|--------|---------|----------|
| VM CPU Usage | < 70% | 70-90% | > 90% |
| CPU Ready | < 5% | 5-10% | > 10% |
| Memory Usage | < 80% | 80-95% | > 95% |
| Memory Balloon | 0 | < 100 MB | > 100 MB |
| Disk Latency | < 10 ms | 10-25 ms | > 25 ms |
| Network Drops | 0 | < 10/min | > 10/min |

---

## Operations Reference (Diagnostic)

| Operation | Description | Parameters |
|-----------|-------------|------------|
| `get_virtual_machine` | VM details + host/datastore | `vm_name` |
| `get_detailed_vm_performance` | Full metrics including disk/network | `vm_name`, `interval`, `metrics` |
| `get_host` | Host details + VM list | `host_name` |
| `get_detailed_host_performance` | Full host metrics | `host_name`, `interval` |
| `get_datastore` | Datastore details | `datastore_name` |
| `get_detailed_datastore_performance` | IOPS, latency, throughput | `datastore_name`, `interval` |
| `get_vm_nics` | Network adapter config | `vm_name` |
| `get_drs_recommendations` | Migration suggestions | `cluster_name` |
| `migrate_vm` | vMotion to another host | `vm_name`, `target_host` |

---

## Success Criteria

- [ ] Multi-turn conversation maintained (context preserved across turns)
- [ ] VM identified and key details cached (host, cluster, datastore)
- [ ] VM performance retrieved with disk I/O and network metrics
- [ ] Host automatically identified from VM data
- [ ] Storage datastore identified from VM data
- [ ] Each diagnostic step builds on previous findings
- [ ] Final summary synthesizes all findings correctly
- [ ] Root cause identified with evidence
- [ ] Actionable recommendations provided
- [ ] No redundant API calls for already-fetched data
- [ ] PerformanceManager API used for detailed metrics
- [ ] Diagnostic highlights show issues automatically
