# Katam Virtualization: architecture and roadmap

Katam Virtualization is a KVM-based virtualization platform for x86 servers. It is built on open, proven parts (KVM/QEMU, libvirt, Ceph, Open vSwitch/OVN). Katam's own code is the control plane, the API, the scheduler, policy, DR orchestration, runbooks and FinOps. Each requirement below names the component that delivers it and the phase it lands in.

## Requirements map

| Requirement | How Katam delivers it | Phase |
|---|---|---|
| Hypervisor on x86 | KVM/QEMU + libvirt on Ubuntu 24.04, q35 machine type, host-passthrough CPU | 1 (done) |
| Linux, Windows, any x86 OS | BIOS or UEFI, Secure Boot + TPM, Hyper-V enlightenments for Windows, ISO install for BSD and appliances | 1 (done) |
| GPU passthrough | VFIO with libvirt managed hostdevs, whole IOMMU group, GPU-aware scheduling | 1 (done) |
| Shared storage | Ceph RBD, 3 replicas, copy-on-write clones | 1 (done) |
| IaC | Versioned REST API (OpenAPI), API tokens, Terraform provider, Ansible collection | 2 |
| Multi-tenancy | Projects, users, roles, quotas, tags | 2 |
| SDN | OVN on every node: overlay (Geneve) and VLAN-backed networks, distributed routing, NAT, floating IPs | 3 |
| Micro-segmentation | OVN ACLs on every VM port: security groups, default-deny east-west, tag-based policy, flow logs | 3 |
| Kubernetes CSI | ceph-csi (RBD) against the Katam Ceph cluster; later a Katam CSI that hot-plugs Katam volumes into VMs | 4 |
| HA | Node health agent, fencing through the iBMC Redfish API, restart on surviving nodes, affinity and anti-affinity rules | 5 |
| DR orchestration | DR plans (VM groups, boot order, re-IP, scripts) run as runbooks; test failover in an isolated network; failback | 5 |
| Zero RPO | Synchronous replication: Ceph stretch mode across two sites plus an arbiter | 5 |
| Runbooks | YAML runbooks: API steps, guest commands, waits, checks, approvals, rollback, dry run, audit trail | 6 |
| FinOps | Usage metering per VM, project and tag; a rate card; showback and chargeback; rightsizing; power cost from iBMC readings | 6 |
| Reports | Scheduled PDF/CSV reports: capacity, cost, DR test results, compliance (segmentation coverage, unprotected VMs) | 6 |

## Components

```
                ┌──────────────────── Katam control plane ────────────────────┐
  UI / CLI ───► │ API (REST, OpenAPI) ─ Scheduler ─ Task + runbook engine     │
  Terraform ──► │ IAM (projects, roles, tokens) ─ Policy ─ DR orchestrator    │
  Ansible   ──► │ Metering + FinOps ─ Reports ─ Postgres (3-node HA, phase 2) │
                └──────┬──────────────┬───────────────┬──────────────┬───────┘
                       │ libvirt      │ OVN NB DB     │ Ceph mgr API │ Redfish (iBMC)
                ┌──────▼──────────────▼───────────────▼──────────────▼───────┐
  Each node:    │ KVM/QEMU   OVS + ovn-controller   Ceph OSDs   katam-agent   │
                └────────────────────────────────────────────────────────────┘
```

## GPU passthrough (phase 1)

Each node reports its PCI GPUs (VGA and 3D controller classes). A GPU passes through together with every endpoint function in its IOMMU group, such as the HDMI audio function; PCI bridges stay with the host. VMs that ask for GPUs only land on nodes with enough free ones, optionally of a given model. A GPU VM is pinned to its node, because device state can't migrate live.

Server prep:
- In BIOS, enable VT-d and MMIO above 4G (needed for large-BAR GPUs).
- Use the GPU riser and a power supply rated for the card.
- Passive data-centre cards need the server fans set for GPU airflow in the iBMC.

`prepare-node.sh` blacklists nouveau and loads VFIO first, so the host never claims a GPU.

Later: NVIDIA vGPU or MIG to split a GPU between VMs (needs licensed vGPU or A100/H100-class cards), and SR-IOV NICs.

## SDN and micro-segmentation (phase 3)

OVN gives distributed routing and firewalling in the OVS datapath on every node, so traffic between two VMs on the same node is still filtered. Katam maps its objects onto OVN like this:

- **Network:** an OVN logical switch. Overlay networks use Geneve tunnels on the storage/migration VLAN. VLAN-backed networks map to VLANs trunked from your physical switches.
- **Router:** an OVN logical router. Gateway ports provide NAT and floating IPs.
- **Security group:** OVN port groups and ACLs. Every VM port is default-deny for east-west traffic, and rules reference other groups or tags (for example, `app=web` may reach `app=db` on 5432).
- **Flow logs:** OVN ACL logging, fed into reports to show segmentation coverage and denied flows.

## Kubernetes (phase 4)

The first step is ceph-csi on the Katam Ceph cluster. Kubernetes clusters running on Katam VMs then get RBD persistent volumes with snapshots and expansion, from the same storage that holds VM disks. The next step is a Katam CSI driver: a PVC becomes a Katam volume hot-plugged into the worker VM through the Katam API, the way vSphere CSI works. A Cluster API provider for creating Kubernetes clusters on Katam would follow.

## HA, DR, RPO and RTO (phase 5)

**Node failure.** katam-agent heartbeats each node. If a node stops answering, Katam powers it off through iBMC Redfish (fencing), confirms it is off, then restarts its VMs on the surviving nodes from Ceph. Expected RTO is about 1–3 minutes: detection, fencing, then guest boot.

**Zero RPO.** This is achievable with synchronous replication: every write is acknowledged only once it is on both sites. Katam uses Ceph stretch mode across two sites plus a small arbiter monitor at a third location. It needs round-trip latency under about 5 ms between sites (same metro), because every disk write pays that latency.

**Remote DR.** For sites further apart, Katam uses RBD snapshot mirroring. RPO is the snapshot interval, from 1 minute upwards.

**Zero RTO is not achievable at the hypervisor layer.** RTO is the time to restore service after a failure. When a host or site dies, the RAM of the VMs running there is gone, so they must boot again elsewhere, which takes seconds to minutes. Near-zero RTO comes from running the application on two or more VMs at once, spread across nodes or sites by anti-affinity rules: database replicas, Windows failover clusters, Kubernetes replicas behind a load balancer. Lockstep VM fault tolerance (QEMU COLO) exists but is experimental and costs a lot of latency, so Katam doesn't promise it.

**DR plans.** A plan lists protected VM groups, boot order, re-IP or network mapping, and pre- and post-scripts. Katam runs it as a runbook with three modes:
- **Test:** boots replicas in an isolated OVN network while production keeps running.
- **Planned migration:** zero data loss.
- **Disaster failover.**

Every run produces a report with the measured RPO and RTO.

## Runbooks (phase 6)

Runbooks are YAML files kept in git and run by the task engine. Each step has a check, an action, verification and an optional rollback. Steps can be Katam API calls, guest commands through qemu-guest-agent, waits, HTTP checks, or manual approvals. Every run supports dry run and leaves an audit trail. DR plans, patch windows and new-tenant onboarding are all runbooks.

## FinOps and reports (phase 6)

**Metering.** A collector reads libvirt domstats, Ceph usage and iBMC power draw every minute into Prometheus or VictoriaMetrics.

**Rate card.** Prices are set per vCPU-hour, GB RAM-hour, GB storage-month and GPU-hour, with optional power pass-through: kWh from the BMC × your tariff, or a solar-offset rate.

**Showback and chargeback.** Usage is reported per project and per tag, and can be exported for billing.

**Rightsizing.** Katam flags idle VMs, oversized CPU or RAM, orphaned volumes and snapshots, and nodes that could be consolidated.

**Reports.** Scheduled PDF/CSV reports cover cost, capacity runway, DR test results and segmentation coverage.

## Status in 0.3

Every phase now has a first working version. "Tested here" means exercised end to end
against libvirt's test driver, SQLite and real qemu-img; "needs your lab" means the code is
written against the documented tools but only real hardware can prove it.

| Area | What's in 0.3 | Tested here | Needs your lab |
|---|---|---|---|
| Core VMs, GPU passthrough | create, power, live/offline move, console, cloud-init, UEFI/TPM Windows | yes | live migration, real GPU |
| Phase 2: API, RBAC, IaC | /api/v1 (OpenAPI), projects, quotas, roles, tokens, katamctl, stack apply, Terraform/Ansible examples | yes | |
| Directory and SSO | AD/LDAP bind with group mapping, SAML 2.0 SP (Entra ID, ADFS, Okta, Keycloak) | metadata, redirect, error paths | real AD and IdP |
| Images, OVA, templates | URL/upload import, OVA (OVF parse), virt-v2v for VMware OVAs, capture VM as template | OVA round trip with qemu-img | virt-v2v, Ceph import |
| Portability | export OVA (VMware vmx-14, streamOptimized VMDK, SHA256 manifest), VMDK, VHDX, qcow2, raw | yes | import into vSphere |
| Phase 3: SDN, micro-segmentation | OVN networks (VLAN localnet, overlay), routers + SNAT, default-deny security groups with group references and deny logging | | all of it |
| Phase 4: Kubernetes | ceph-csi Helm values from the API | | |
| Phase 5: HA, witness, DR | health checks, witness second opinion, Redfish fencing, HA restart, safe recovery, maintenance evacuation; DR plans with rbd-mirror test/planned/disaster and stretch mode | HA restart and recovery, witness | Redfish, rbd-mirror, stretch |
| Phase 6: runbooks, FinOps, reports | YAML runbooks with retries/rollback/dry run, metering, costs, rightsizing, power via Redfish, 7 reports (CSV, HTML) | yes | BMC power readings |
| Snapshots and immutable vault (0.4) | VM snapshots (RBD/qcow2, guest-agent quiesce), schedules with pruning, retention locks, VM-delete guard, S3 Object Lock COMPLIANCE copies, vault restore, coverage report | snapshots, locks, schedules on the test driver; vault copy, lock enforcement and point-in-time data against an S3 mock | Ceph RBD snapshots, a real MinIO/S3 bucket |
| Installer | three .deb packages, ISO builder with node/controller/all-in-one/witness boot entries | packages install on Ubuntu 24.04; ISO layout | booting the real ISO |

## Build order

1. Core platform, Ceph, consoles, GPU passthrough (this release)
2. API v1 + IAM + Terraform/Ansible, Postgres, HA control plane
3. OVN SDN + micro-segmentation
4. ceph-csi integration, then Katam CSI
5. HA with Redfish fencing, metro and async DR, DR plans
6. Runbook engine, FinOps, reports
