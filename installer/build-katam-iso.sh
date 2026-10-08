#!/usr/bin/env bash
# Build the Katam Virtualization installer ISO for any x86-64 server (BIOS or UEFI).
#
# It takes the official Ubuntu 24.04 live-server ISO, adds the Katam packages and an
# automated installer that asks only for disk, network and login, and adds boot menu
# entries for each role:
#
#   Katam node         KVM host (run katam-node-setup after first boot)
#   Katam controller   web UI, API, HA, DR, FinOps (a small VM or box)
#   Katam all-in-one   controller and node on one server (single-server labs)
#   Katam witness      tiny HA tie-breaker for a third location
#
# Run on any Ubuntu/Debian machine (or WSL) with ~6 GB free:
#   sudo apt install xorriso curl
#   ./build-katam-iso.sh                       # downloads Ubuntu 24.04 and builds
#   ./build-katam-iso.sh --iso ubuntu-24.04.3-live-server-amd64.iso
#
# Output: katam-virtualization-0.4.0-amd64.iso. Write it to USB with
#   sudo dd if=katam-virtualization-0.4.0-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
# or mount it as virtual media through the server's BMC (iBMC, iDRAC, iLO, XCC, IPMI).
set -euo pipefail

VER=${VER:-0.4.0}
HERE=$(cd "$(dirname "$0")" && pwd)
DEBS="$HERE/../packaging/dist"
UBUNTU_URL=${UBUNTU_URL:-https://releases.ubuntu.com/24.04/ubuntu-24.04.3-live-server-amd64.iso}
ISO="" OUT="$HERE/katam-virtualization-$VER-amd64.iso"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso) ISO="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --debs) DEBS="$2"; shift 2 ;;
    *) echo "Unknown option $1" >&2; exit 1 ;;
  esac
done
command -v xorriso >/dev/null || { echo "Install xorriso first: sudo apt install xorriso" >&2; exit 1; }
ls "$DEBS"/katam-*.deb >/dev/null 2>&1 || { echo "No packages in $DEBS. Run packaging/build-debs.sh first." >&2; exit 1; }

if [[ -z "$ISO" ]]; then
  ISO="$HERE/$(basename "$UBUNTU_URL")"
  if [[ ! -f "$ISO" ]]; then
    echo "== Downloading $(basename "$UBUNTU_URL")"
    curl -fL --retry 3 -o "$ISO.part" "$UBUNTU_URL" && mv "$ISO.part" "$ISO"
    SUMS=$(curl -fsSL "$(dirname "$UBUNTU_URL")/SHA256SUMS" || true)
    if [[ -n "$SUMS" ]]; then
      want=$(echo "$SUMS" | awk -v f="$(basename "$ISO")" '$2=="*"f || $2==f {print $1}')
      got=$(sha256sum "$ISO" | cut -d' ' -f1)
      [[ -z "$want" || "$want" == "$got" ]] || { echo "Checksum mismatch for $ISO" >&2; exit 1; }
      echo "   checksum OK"
    fi
  fi
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
echo "== Reading boot menu from $(basename "$ISO")"
xorriso -osirrox on -indev "$ISO" -extract /boot/grub/grub.cfg "$WORK/grub.cfg" >/dev/null 2>&1
chmod u+w "$WORK/grub.cfg"

mkdir -p "$WORK/add/katam/debs"
cp "$DEBS"/katam-*.deb "$WORK/add/katam/debs/"

# ---- autoinstall seeds, one per role -------------------------------------
role_seed() {   # role, packages to install, extra late commands
  local role=$1 pkgs=$2 extra=$3
  mkdir -p "$WORK/add/nocloud/$role"
  : > "$WORK/add/nocloud/$role/meta-data"
  cat > "$WORK/add/nocloud/$role/user-data" <<EOF
#cloud-config
autoinstall:
  version: 1
  # The installer asks for these three; everything else is automatic.
  interactive-sections: [network, storage, identity]
  locale: en_US.UTF-8
  keyboard: {layout: us}
  source: {id: ubuntu-server-minimal}
  ssh: {install-server: true, allow-pw: true}
  storage:
    layout: {name: lvm, sizing-policy: all}
  packages: [$pkgs]
  late-commands:
    - mkdir -p /target/var/cache/katam
    - cp /cdrom/katam/debs/*.deb /target/var/cache/katam/
    - echo "$role" > /target/etc/katam-role
$extra
    - |
      cat > /target/etc/update-motd.d/99-katam <<'MOTD'
      #!/bin/sh
      echo
      echo "  Katam Virtualization $VER, role: \$(cat /etc/katam-role)"
      echo "  Install guide: /usr/share/doc/katam-controller or the Word guide."
      [ -f /root/katam-initial-admin.txt ] && echo "  Admin sign-in details: sudo cat /root/katam-initial-admin.txt"
      [ "\$(cat /etc/katam-role)" = node ] && echo "  Next: sudo katam-node-setup --controller-key '<key>' --controller-ip <ip> --peers '<ips>'"
      echo
      MOTD
    - chmod 755 /target/etc/update-motd.d/99-katam
EOF
}
APT="    - curtin in-target -- bash -c 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y"
role_seed node "openssh-server, chrony, qemu-system-x86, qemu-utils, libvirt-daemon-system, ovmf, ceph-common" \
"$APT /var/cache/katam/katam-node_*.deb'"
role_seed controller "openssh-server, chrony" \
"$APT /var/cache/katam/katam-controller_*.deb'"
role_seed allinone "openssh-server, chrony, qemu-system-x86, qemu-utils, libvirt-daemon-system, ovmf" \
"$APT /var/cache/katam/katam-controller_*.deb /var/cache/katam/katam-node_*.deb'"
role_seed witness "openssh-server, chrony" \
"$APT /var/cache/katam/katam-witness_*.deb'"

# ---- boot menu ------------------------------------------------------------
entries=""
for pair in "node:Katam node (KVM host)" "controller:Katam controller" "allinone:Katam all-in-one (single server)" "witness:Katam witness"; do
  role=${pair%%:*}; label=${pair#*:}
  entries+="menuentry \"Install $label\" {
	set gfxpayload=keep
	linux	/casper/vmlinuz autoinstall ds=nocloud\\;s=/cdrom/nocloud/$role/ ---
	initrd	/casper/initrd
}
"
done
{
  echo "set default=0"
  echo "set timeout=30"
  printf '%s' "$entries"
  sed -e '/^set default=/d' -e '/^set timeout=/d' "$WORK/grub.cfg" | sed '/^menuentry/,$!d'
} > "$WORK/grub.new"
# keep the original header (fonts, theme) above our entries
sed '/^menuentry/,$d' "$WORK/grub.cfg" | sed -e '/^set default=/d' -e '/^set timeout=/d' > "$WORK/grub.head"
cat "$WORK/grub.head" "$WORK/grub.new" > "$WORK/add/grub.cfg"

echo "== Writing $(basename "$OUT")"
rm -f "$OUT"
xorriso -indev "$ISO" -outdev "$OUT" \
  -map "$WORK/add/katam" /katam \
  -map "$WORK/add/nocloud" /nocloud \
  -map "$WORK/add/grub.cfg" /boot/grub/grub.cfg \
  -volid "KATAM_${VER//./_}" \
  -boot_image any replay 2>&1 | grep -E "^xorriso : (FAILURE|SORRY)" && exit 1 || true
sha256sum "$OUT" > "$OUT.sha256"
echo
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
echo "Boot it on each server and pick the role from the menu. Servers need internet"
echo "access during install to fetch Ubuntu packages."
