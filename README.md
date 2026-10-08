# Katam Virtualization 0.5.1

A virtualization platform for x86-64 servers, built on KVM, libvirt, Ceph and OVN.
One web UI and REST API for VMs, images and OVAs, templates, GPU passthrough,
micro-segmentation, HA with fencing and a witness, snapshots with retention locks and
immutable S3 vault copies, DR, runbooks, FinOps and reports,
with sign-in through Active Directory or SAML single sign-on.

New in 0.5: the control plane can run on every node, with **leader election** through etcd and
a replicated PostgreSQL ([docs/CLUSTER.md](docs/CLUSTER.md)); a **witness** breaks ties for
two-node clusters and between PR and DR sites; **Prometheus metrics and a Grafana dashboard**
([docs/MONITORING.md](docs/MONITORING.md)); and **runbooks managed in Git** with CI/CD
([examples/gitops](examples/gitops/README.md)). 0.5.1 adds **one-click upgrades** of the whole platform
from the UI.

**Download:** [latest installer ISO](https://github.com/RaymondKatam/katam/releases/latest) (free, Apache-2.0).

Quick install: [docs/INSTALL.md](docs/INSTALL.md). Full step-by-step guide: **Katam-Virtualization-Install-Guide.docx** in each release. Maintainers: [docs/RELEASING.md](docs/RELEASING.md).

## What's here

| Path | What it is |
|---|---|
| `controller/katam/` | Control plane (Python, Flask): API, UI, HA, DR, SDN, FinOps |
| `node-setup/` | Node prep (`katam-node-setup`), `katam-cluster` (etcd + Patroni control plane), Ceph, OVN, RBD mirroring, stretch cluster, netplan examples |
| `packaging/build-debs.sh` | Builds `katam-controller`, `katam-node`, `katam-witness` .deb packages |
| `installer/build-offline-iso.sh` | Builds the offline installer ISO from Ubuntu 24.04 packages |
| `installer/iso/katam-install` | The installer that runs from the ISO (interactive or unattended) |
| `.github/workflows/release.yml` | Tag a version to build and publish the ISO, packages, apt repo and update feed |
| `release/` | Apt repository publisher, update-feed generator, release notes template |
| `website/download.html` | Download page for a blog or GitHub Pages (reads the update feed) |
| `examples/` | Stack file for `katamctl apply`, Terraform, Ansible, runbooks, a GitOps runbook repo with CI pipelines |
| `integrations/` | Grafana dashboard and provisioning, Prometheus scrape job and alert rules |
| `controller/tests/` | Tests (`python3 -m pytest controller/tests`; leader election tests start a real etcd) |
| `config.example.yaml` | Every setting, commented |

## Quick start

Boot each server from **katam-virtualization-0.5.1-amd64.iso** (USB or BMC virtual media,
BIOS or UEFI, Secure Boot off). The installer asks for a role (node, controller,
all-in-one, witness), the OS disk, the management network and an admin account, then
installs offline: everything is on the ISO. Unattended installs take `katam.*` boot
parameters (guide section 4.4).

On nodes, run `katam-node-setup` with the controller's SSH key. On the controller, edit
`/etc/katam/config.yaml`, restart `katam`, and sign in with the password in
`/root/katam-initial-admin.txt`.

Rebuild the ISO yourself (Ubuntu 24.04, 8 GB free, access to archive.ubuntu.com):

```bash
sudo installer/build-offline-iso.sh
```

`installer/build-katam-iso.sh` is the older alternative: it remasters Ubuntu's own
server ISO and needs internet during installation.

Already running Ubuntu 24.04? Skip the ISO:
`sudo apt install ./katam-controller_0.5.1_all.deb` (or `katam-node`, `katam-witness`).

## Hardware

Any x86-64 server with Intel VT-x/VT-d or AMD-V/AMD-Vi: Huawei, Dell, HPE, Lenovo,
Supermicro or white boxes. Fencing and power metering use Redfish, which iBMC, iDRAC,
iLO, XClarity and current Supermicro BMCs all speak. Three or more nodes for Ceph and
HA; one node works in all-in-one mode with local disks.

## Moving VMs to other platforms

Export a stopped VM as OVA (VMware), VHDX (Hyper-V) or qcow2 (Proxmox, OpenStack).
Install open-vm-tools or VMware Tools in the guest before importing into vSphere;
Windows guests need the LSI Logic SAS or PVSCSI driver present. Coming the other way,
import VMware OVAs with the virt-v2v option so virtio drivers are injected.
