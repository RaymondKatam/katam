## Katam Virtualization 0.4.0

The first public release. A free, open-source virtualization platform for x86-64 servers, built on KVM, libvirt, Ceph and OVN, with one web UI and REST API over all of it.

**Download** `katam-virtualization-0.4.0-amd64.iso` below (about 600 MB, BIOS and UEFI, installs offline) and check it:

```bash
shasum -a 256 -c katam-virtualization-0.4.0-amd64.iso.sha256    # macOS
sha256sum -c katam-virtualization-0.4.0-amd64.iso.sha256        # Linux
```

Quick install: [docs/INSTALL.md](https://github.com/RaymondKatam/katam/blob/main/docs/INSTALL.md). Full guide: `Katam-Virtualization-Install-Guide.docx`, attached below.

### What's in it
- VMs: Linux, Windows (UEFI, Secure Boot, TPM 2.0) and other guests; GPU passthrough; live migration
- Images: import qcow2, raw, VMDK, VHD/VHDX and OVA; templates; export to OVA (VMware), VHDX (Hyper-V) and qcow2
- Access: projects, quotas, roles, API tokens; Active Directory (LDAP) and SAML single sign-on
- Networking: VLAN bridges or OVN networks with routing, NAT and default-deny security groups
- Resilience: HA restart with BMC fencing and a witness; DR with Ceph stretch clusters or RBD mirroring
- Data protection: scheduled snapshots with retention locks; immutable S3 Object Lock copies
- Operations: REST API, `katamctl` CLI, declarative stack files, runbooks, cost tracking, reports

### Status
Early software (0.x). Try it in a lab before trusting it with production workloads, and keep backups. Bug reports and pull requests are welcome.
