#!/usr/bin/env bash
# Prepare any x86-64 server running Ubuntu 24.04 Server as a Katam KVM node.
# Works on Intel (VT-x/VT-d) and AMD (AMD-V/AMD-Vi) CPUs, any vendor's hardware
# (Huawei, Dell, HPE, Lenovo, Supermicro, white boxes).
#
#   sudo katam-node-setup \
#     --controller-key "ssh-ed25519 AAAA... katam@controller" \
#     --controller-ip 172.20.10.5 \
#     --peers "172.20.30.11 172.20.30.12 172.20.30.13" \
#     [--storage rbd|dir] [--sdn --ovn-central 172.20.10.11 --encap-ip 172.20.30.11 [--provider-bridge br-provider]]
#
# Safe to re-run.
set -euo pipefail

CONTROLLER_KEY="" CONTROLLER_IP="" PEERS="" STORAGE="rbd" SDN=0 OVN_CENTRAL="" ENCAP_IP="" PROVIDER_BR="br-provider"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --controller-key) CONTROLLER_KEY="$2"; shift 2 ;;
    --controller-ip)  CONTROLLER_IP="$2"; shift 2 ;;
    --peers)          PEERS="$2"; shift 2 ;;
    --storage)        STORAGE="$2"; shift 2 ;;
    --sdn)            SDN=1; shift ;;
    --ovn-central)    OVN_CENTRAL="$2"; shift 2 ;;
    --encap-ip)       ENCAP_IP="$2"; shift 2 ;;
    --provider-bridge) PROVIDER_BR="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ -n "$CONTROLLER_KEY" && -n "$CONTROLLER_IP" ]] || { echo "--controller-key and --controller-ip are required." >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "Katam nodes must be x86-64." >&2; exit 1; }
if [[ $SDN -eq 1 && ( -z "$OVN_CENTRAL" || -z "$ENCAP_IP" ) ]]; then
  echo "--sdn needs --ovn-central and --encap-ip." >&2; exit 1
fi

echo "== CPU virtualization"
if grep -qw vmx /proc/cpuinfo; then
  VENDOR=intel; IOMMU="intel_iommu=on iommu=pt"
elif grep -qw svm /proc/cpuinfo; then
  VENDOR=amd; IOMMU="amd_iommu=on iommu=pt"
else
  cat >&2 <<'MSG'
Hardware virtualization is off. Open the server's firmware setup (through its BMC
remote console: iBMC, iDRAC, iLO, XClarity or IPMI) and enable:
  Intel: "Intel Virtualization Technology" (VT-x) and "VT-d"
  AMD:   "SVM Mode" and "IOMMU"
Then reboot and run this again.
MSG
  exit 1
fi
echo "   $VENDOR CPU, $(nproc) threads, $(awk '/MemTotal/{printf "%.0f GB", $2/1048576}' /proc/meminfo) RAM"

echo "== Packages"
PKGS=(qemu-system-x86 qemu-utils qemu-block-extra ipxe-qemu libvirt-daemon-system libvirt-clients
      ovmf swtpm swtpm-tools ceph-common lvm2 chrony tuned cpu-checker ufw uuid-runtime
      curl xorriso)
[[ $SDN -eq 1 ]] && PKGS+=(openvswitch-switch ovn-host)
MISSING=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  apt-get update || { echo "Need internet (or a local mirror) for: ${MISSING[*]}" >&2; exit 1; }
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING[@]}"
else
  echo "   all required packages already installed"
fi
# virt-v2v converts VMware OVAs; optional and large, so it's fetched only when online.
if ! command -v virt-v2v >/dev/null; then
  if timeout 20 apt-get update >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y virt-v2v libguestfs-tools || true
  else
    echo "   offline: skipping virt-v2v (install later with: apt install virt-v2v)"
  fi
fi
kvm-ok

echo "== Kernel settings"
REBOOT=0
if ! grep -q 'iommu=pt' /etc/default/grub; then
  sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"\(.*\)\"/GRUB_CMDLINE_LINUX_DEFAULT=\"\1 $IOMMU\"/" /etc/default/grub
  update-grub
  REBOOT=1
fi
echo "options kvm_$VENDOR nested=1" > /etc/modprobe.d/kvm-nested.conf
cat > /etc/sysctl.d/90-katam.conf <<'CONF'
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
net.bridge.bridge-nf-call-arptables = 0
vm.swappiness = 10
CONF
cat > /etc/modprobe.d/katam-gpu.conf <<'CONF'
blacklist nouveau
options nouveau modeset=0
softdep nvidia pre: vfio-pci
softdep amdgpu pre: vfio-pci
CONF
printf 'vfio\nvfio_iommu_type1\nvfio_pci\n' > /etc/modules-load.d/vfio.conf
update-initramfs -u
modprobe br_netfilter || true
sysctl --system >/dev/null
tuned-adm profile virtual-host
systemctl enable --now chrony libvirtd

echo "== Controller account"
id katam >/dev/null 2>&1 || useradd -m -s /bin/bash -G libvirt,kvm katam
install -d -m 700 -o katam -g katam /home/katam/.ssh
AK=/home/katam/.ssh/authorized_keys
touch "$AK"
grep -qF "$CONTROLLER_KEY" "$AK" || echo "$CONTROLLER_KEY" >> "$AK"
chown katam:katam "$AK"; chmod 600 "$AK"
# The controller runs a fixed set of tools as root: disk conversion, Ceph, OVN.
cat > /etc/sudoers.d/katam <<'CONF'
Cmnd_Alias KATAM = /usr/bin/qemu-img, /usr/bin/rbd, /usr/bin/ceph, /usr/bin/ovn-nbctl, \
  /usr/bin/ovs-vsctl, /usr/bin/virt-v2v, /usr/bin/curl, /usr/bin/tar, /usr/bin/gunzip, \
  /usr/bin/mkdir, /usr/bin/chmod, /usr/bin/rm, /usr/bin/mv, /usr/bin/tee, /usr/bin/stat, \
  /usr/bin/sha256sum, /usr/bin/cat, /usr/bin/ls, /usr/bin/apt-get, /usr/sbin/reboot
katam ALL=(root) NOPASSWD: KATAM
CONF
chmod 440 /etc/sudoers.d/katam
visudo -cf /etc/sudoers.d/katam >/dev/null

echo "== Directories and libvirt storage pools"
install -d -m 755 /var/lib/katam /var/lib/katam/staging /var/lib/katam/images
mkpool() {
  local name=$1 path=$2
  mkdir -p "$path"
  virsh pool-info "$name" >/dev/null 2>&1 || virsh pool-define-as "$name" dir --target "$path"
  virsh pool-autostart "$name" >/dev/null
  virsh pool-info "$name" | grep -q running || virsh pool-start "$name"
}
mkpool seeds /var/lib/katam/seeds
mkpool isos  /var/lib/katam/isos
[[ "$STORAGE" == "dir" ]] && mkpool vmpool /var/lib/katam/images
virsh net-autostart --disable default >/dev/null 2>&1 || true
virsh net-destroy default >/dev/null 2>&1 || true

if [[ $SDN -eq 1 ]]; then
  echo "== OVN (SDN)"
  systemctl enable --now openvswitch-switch ovn-controller
  ovs-vsctl set open_vswitch . \
    external_ids:ovn-remote="tcp:${OVN_CENTRAL}:6642" \
    external_ids:ovn-encap-type=geneve \
    external_ids:ovn-encap-ip="$ENCAP_IP" \
    external_ids:system-id="$(hostname)" \
    external_ids:ovn-bridge-mappings="provider:${PROVIDER_BR}"
  ovs-vsctl --may-exist add-br br-int -- set bridge br-int fail-mode=secure
  ovs-vsctl --may-exist add-br "$PROVIDER_BR"
  echo "   Add your VLAN trunk to $PROVIDER_BR (see netplan-ovs-example.yaml), e.g.:"
  echo "   ovs-vsctl --may-exist add-port $PROVIDER_BR bond0"
fi

echo "== Firewall"
ufw allow OpenSSH
ufw allow from "$CONTROLLER_IP" to any port 5900:5999 proto tcp comment 'VNC via controller'
for p in $PEERS; do
  ufw allow from "$p" to any port 49152:49215 proto tcp comment 'live migration'
  ufw allow from "$p" to any port 3300,6789 proto tcp comment 'ceph mon'
  ufw allow from "$p" to any port 6800:7300 proto tcp comment 'ceph osd/mgr'
  ufw allow from "$p" to any port 6081 proto udp comment 'OVN geneve'
  ufw allow from "$p" to any port 6641,6642 proto tcp comment 'OVN databases'
done
ufw --force enable

echo
echo "Done on $(hostname) ($VENDOR)."
[[ $REBOOT -eq 1 ]] && echo "Reboot now so the IOMMU settings take effect."
exit 0
