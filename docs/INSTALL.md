# Installing Katam Virtualization

Katam Virtualization is an open-source virtualization platform for x86-64 servers, built on
KVM, libvirt, Ceph and OVN. This page is the short version; the full step-by-step guide is
`Katam-Virtualization-Install-Guide.docx` in each release.

## Requirements

| Item | Minimum | Recommended |
|---|---|---|
| Servers | 1 (all-in-one, no HA) | 3 or more (Ceph, HA, live migration) |
| CPU | x86-64 with VT-x/VT-d or AMD-V/AMD-Vi | Two sockets |
| RAM per node | 32 GB | 128 GB or more |
| Disks | One OS disk | RAID 1 OS mirror, plus empty SSD/NVMe data disks for Ceph in HBA/JBOD mode |
| Network | 1 GbE | 2 x 10/25 GbE in an LACP bond, separate VLANs for management, VMs and storage |
| BMC | Optional | Redfish-capable (iDRAC, iLO, XClarity, iBMC, Supermicro) for fencing and power data |

## 1. Prepare the servers

- In firmware, enable **VT-x and VT-d** (Intel) or **SVM and IOMMU** (AMD), and **Above 4G decoding** if you'll pass through GPUs.
- Turn **Secure Boot off** on the host (the installer's boot loader isn't Microsoft-signed; Secure Boot inside Windows VMs still works).
- Put Ceph data disks in HBA/JBOD/pass-through mode and leave them empty.

## 2. Install from the ISO

1. Download the ISO and check it: `sha256sum -c katam-virtualization-<version>-amd64.iso.sha256`
2. Write it to USB (`sudo dd if=<iso> of=/dev/sdX bs=4M status=progress oflag=sync`, or Rufus/balenaEtcher in DD mode), or mount it as BMC virtual media.
3. Boot it. The installer asks for:
   - **Role**: node (runs VMs), controller (web UI and API), all-in-one, or witness (HA tie-breaker at a third site)
   - **Disk**: the OS disk. Everything on it is erased.
   - **Network**: management interface, optional VLAN, static IP or DHCP
   - **Hostname** and an **admin account**
4. It installs offline in 5 to 15 minutes and reboots.

Unattended install: press `e` on the boot menu and append, for example,
`katam.auto=1 katam.role=node katam.disk=/dev/sda katam.iface=eno1 katam.ip=172.20.10.11/24 katam.gw=172.20.10.1 katam.hostname=hv1 katam.password=Change-Me-Now`.
Required: `auto`, `role`, `disk`, `iface`, `password`. Change the password afterwards.

Already running Ubuntu Server 24.04? Install the packages instead: `sudo apt install ./katam-node_<version>_all.deb` (or `katam-controller`, `katam-witness`).

## 3. Join nodes to the controller

On the controller, copy its SSH key: `sudo cat /var/lib/katam/.ssh/id_ed25519.pub`. On each node:

```bash
sudo katam-node-setup \
  --controller-key "ssh-ed25519 AAAA... katam@controller" \
  --controller-ip 172.20.10.5 \
  --peers "172.20.30.11 172.20.30.12 172.20.30.13"
sudo reboot
```

Set up the bond and VLANs with the netplan examples in `/usr/share/katam-node/` (do it from the BMC console).

## 4. Storage

- **Ceph (3+ nodes):** on the first node run `sudo /usr/share/katam-node/ceph-bootstrap.sh <mon-ip> <storage-subnet> <host:ip> ...`. Run the printed `rbd-pool.sh` command on every node and paste the printed `storage:` block into the controller's config.
- **Local disks (1 node or no Ceph):** `storage.type: dir` in the config and `--storage dir` for katam-node-setup. No live migration or HA restart in this mode.

## 5. Configure and sign in

1. On the controller, edit `/etc/katam/config.yaml`: nodes (management IP, libvirt URI, storage IP, BMC), storage and networks. Every setting is commented.
2. Accept each node's SSH host key once: `sudo -u katam ssh -i /var/lib/katam/.ssh/id_ed25519 katam@<node-ip> virsh list`
3. `sudo systemctl restart katam katam-console`
4. Open `http://<controller-ip>:8080` and sign in as `admin` with the password from `/root/katam-initial-admin.txt`. Change it under Access.
5. Put HTTPS in front with nginx or Caddy before exposing it beyond your lab.

## Next steps

Import a cloud image (Images and templates > Import, e.g. the Ubuntu 24.04 cloud image URL), make a template, and create your first VM. The full guide covers Active Directory and SAML sign-in, GPUs, SDN and security groups, HA and the witness, DR, runbooks, costs and reports, and moving VMs to VMware or Hyper-V.

## No separate controller: leader election (0.5)

Instead of one controller VM, run the controller on every node: the nodes elect a leader through
etcd, share a replicated PostgreSQL, and a virtual IP follows the leader. Two-node clusters add
a witness as the tie-breaker. Setup is three commands (`katam-cluster init`, copy the bundle,
`katam-cluster join` on each node); see [CLUSTER.md](CLUSTER.md).

## Grafana and runbooks in Git (0.5)

Prometheus scrapes `/metrics`; import the bundled Grafana dashboard
([MONITORING.md](MONITORING.md)). Keep runbooks in a Git repository with CI checks and a push
webhook ([examples/gitops](../examples/gitops/README.md)).

## Snapshots and immutable backups

Take snapshots from a VM's **Snapshots** button or on a schedule (Protection tab). Locked
snapshots can't be deleted through Katam before their lock ends. For ransomware protection,
point `protection.vault` in the config at an S3 bucket with Object Lock (MinIO, Ceph RGW,
AWS, Wasabi): copies there can't be deleted by anyone until their retention date.

## Updates

One click. When a release is out, admins see **Upgrade now** at the top of the Katam UI. It upgrades
every node in turn (VMs move away first), then the controllers, and the page reconnects on the new
version. Running VMs keep running. Servers installed from 0.5.1 or later are already on the signed
update channel; for older installs run this once on each server:

```bash
curl -fsSL https://raymondkatam.github.io/katam/enable-updates.sh | sudo sh
```

## Status

Katam is early (0.x) software. Test it in a lab before running production workloads, and keep backups.
