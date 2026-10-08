#!/usr/bin/env bash
# Build the self-contained Katam Virtualization installer ISO from Ubuntu 24.04 packages.
#
# Unlike build-katam-iso.sh (which remasters Ubuntu's own ISO), this builds the whole
# system from archive.ubuntu.com, so the result installs with NO internet access:
# kernel and firmware, KVM/QEMU, libvirt, Ceph client and cephadm, Open vSwitch/OVN,
# the controller's Python stack and the three Katam packages are all on the ISO.
#
#   sudo apt install debootstrap squashfs-tools xorriso grub-pc-bin grub-efi-amd64-bin mtools dosfstools
#   sudo installer/build-offline-iso.sh            # about 15-30 minutes, needs ~8 GB free
#
# Output: installer/katam-virtualization-0.4.0-amd64.iso (BIOS and UEFI, CD/DVD and USB).
set -euo pipefail

VER=${VER:-0.5.1}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
WORK=${WORK:-$HERE/build}
MIRROR=${MIRROR:-http://archive.ubuntu.com/ubuntu}
OUT=${OUT:-$HERE/katam-virtualization-$VER-amd64.iso}
C=$WORK/chroot
ISO=$WORK/iso
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }

PKGS=(
  # system
  linux-generic casper systemd-sysv netplan.io systemd-resolved openssh-server sudo whiptail
  gdisk parted dosfstools e2fsprogs squashfs-tools lvm2 mdadm chrony curl wget nano vim-tiny less
  iputils-ping iproute2 ethtool pciutils usbutils lshw dmidecode bash-completion tmux rsync
  grub-pc-bin grub-efi-amd64-bin grub2-common efibootmgr locales tzdata ca-certificates
  ipmitool smartmontools nvme-cli sg3-utils jq
  # virtualization node
  qemu-system-x86 qemu-utils qemu-block-extra ipxe-qemu seabios ovmf swtpm swtpm-tools
  libvirt-daemon-system libvirt-clients tuned cpu-checker ufw uuid-runtime xorriso
  ceph-common cephadm podman openvswitch-switch ovn-host ovn-central
  # control plane on the nodes: leader election, replicated database, virtual IP
  etcd-server etcd-client patroni postgresql postgresql-client iputils-arping git
  # controller
  python3-flask python3-waitress python3-yaml python3-sqlalchemy python3-libvirt python3-pycdlib
  python3-ldap3 python3-onelogin-saml2 python3-requests python3-boto3 python3-lxml python3-xmlsec python3-psycopg2
  novnc python3-websockify openssh-client openssl sqlite3
)

mkdir -p "$WORK"
cleanup() { for m in dev/pts dev proc sys run; do umount -lf "$C/$m" 2>/dev/null || true; done; }
trap cleanup EXIT

echo "== 1/6 Base system"
if [[ ! -x $C/usr/bin/apt-get ]]; then
  debootstrap --arch=amd64 --components=main,universe --include=ca-certificates,gnupg noble "$C" "$MIRROR"
fi

echo "== 2/6 Packages"
cat > "$C/etc/apt/sources.list" <<EOF
deb $MIRROR noble main universe
deb $MIRROR noble-updates main universe
deb $MIRROR noble-security main universe
EOF
for m in dev dev/pts proc sys run; do mkdir -p "$C/$m"; mount --bind "/$m" "$C/$m"; done
rm -f "$C/etc/resolv.conf"; cp -L /etc/resolv.conf "$C/etc/resolv.conf"
cat > "$C/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod +x "$C/usr/sbin/policy-rc.d"
cat > "$C/etc/default/grub" <<'EOF'
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="Katam Virtualization"
GRUB_CMDLINE_LINUX_DEFAULT=""
GRUB_CMDLINE_LINUX=""
EOF
# Builds behind a proxy: pass it to apt inside the chroot.
PROXY=${http_proxy:-${HTTP_PROXY:-}}
if [[ -n $PROXY ]]; then
  printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$PROXY" "${https_proxy:-${HTTPS_PROXY:-$PROXY}}" > "$C/etc/apt/apt.conf.d/99build-proxy"
fi
# PostgreSQL and etcd stay idle until katam-cluster configures them: no default database
# cluster on port 5432 and no stand-alone etcd on an installed single server.
mkdir -p "$C/etc/postgresql-common/createcluster.d"
echo 'create_main_cluster = false' > "$C/etc/postgresql-common/createcluster.d/katam.conf"
chroot "$C" bash -c "set -e; export DEBIAN_FRONTEND=noninteractive LANG=C
  apt-get update -q
  apt-get -y -q dist-upgrade
  apt-get install -y -q --no-install-recommends ${PKGS[*]}
  locale-gen en_US.UTF-8 >/dev/null
  apt-get clean"

rm -f "$C/etc/apt/apt.conf.d/99build-proxy"

echo "== 3/6 Katam packages and installer"
[[ -f $ROOT/packaging/dist/katam-controller_${VER}_all.deb ]] || "$ROOT/packaging/build-debs.sh"
mkdir -p "$C/opt/katam-debs"
rm -f "$C"/opt/katam-debs/*.deb
cp "$ROOT"/packaging/dist/katam-*_${VER}_all.deb "$C/opt/katam-debs/"
sed "s/^VER=.*/VER=$VER/" "$HERE/iso/katam-install" > "$C/usr/local/sbin/katam-install"
chmod 755 "$C/usr/local/sbin/katam-install"
rm -rf "$C/opt/katam-node-setup"
cp -r "$ROOT/node-setup" "$C/opt/katam-node-setup"
echo "Katam Virtualization $VER live installer" > "$C/etc/katam-live"
echo "katam-installer" > "$C/etc/hostname"
mkdir -p "$C/etc/systemd/system/getty@tty1.service.d"
cat > "$C/etc/systemd/system/getty@tty1.service.d/zz-katam-live.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
EOF
cat > "$C/root/.bash_profile" <<'EOF'
if [ -f /etc/katam-live ] && [ "$(tty)" = /dev/tty1 ] && [ -z "${KATAM_SHELL:-}" ]; then
  /usr/local/sbin/katam-install
  echo; echo "Installer exited. Type katam-install to start again."
fi
EOF
cat > "$C/etc/netplan/01-live.yaml" <<'EOF'
network:
  version: 2
  ethernets:
    all:
      match: {name: "e*"}
      dhcp4: true
      optional: true
EOF
chmod 600 "$C/etc/netplan/01-live.yaml"
# Live session: create SSH host keys at boot (the image ships none, so every
# installed server gets its own). Skipped once installed (no /etc/katam-live).
cat > "$C/etc/systemd/system/katam-live-hostkeys.service" <<'UNIT'
[Unit]
Description=Create SSH host keys for the Katam live installer
ConditionPathExists=/etc/katam-live
Before=ssh.service
[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
[Install]
WantedBy=multi-user.target
UNIT
chroot "$C" systemctl enable katam-live-hostkeys.service >/dev/null 2>&1
# OVS isn't running in the live session; the installer removes this mask.
ln -sf /dev/null "$C/etc/systemd/system/netplan-ovs-cleanup.service"
# Installed systems get fresh host keys and a new machine-id from the installer.
rm -f "$C"/etc/ssh/ssh_host_* "$C/usr/sbin/policy-rc.d"
: > "$C/etc/machine-id"
chroot "$C" systemctl enable ssh systemd-networkd >/dev/null 2>&1 || true
chroot "$C" systemctl disable apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
chroot "$C" systemctl disable etcd >/dev/null 2>&1 || true   # katam-cluster enables it
"$HERE/slim-image.sh" "$C"     # server firmware only; keeps the ISO a single ~600 MB file
chroot "$C" update-initramfs -u -k all

echo "== 4/6 Compressing the system"
rm -rf "$ISO"; mkdir -p "$ISO/casper" "$ISO/boot/grub" "$ISO/.disk"
KVER=$(ls "$C/boot" | sed -n 's/^vmlinuz-//p' | sort -V | tail -1)
cp "$C/boot/vmlinuz-$KVER" "$ISO/casper/vmlinuz"
cp "$C/boot/initrd.img-$KVER" "$ISO/casper/initrd"
cleanup
rm -f "$C/etc/resolv.conf"; ln -s ../run/systemd/resolve/stub-resolv.conf "$C/etc/resolv.conf"
rm -rf "$C"/tmp/* "$C"/var/cache/apt/archives/*.deb
mkdir -p "$C/tmp" "$C/var/crash"; chmod 1777 "$C/tmp"
mksquashfs "$C" "$ISO/casper/filesystem.squashfs" -noappend -comp xz -b 1M -e boot/efi
du -sx --block-size=1 "$C" | cut -f1 > "$ISO/casper/filesystem.size"
echo "Katam Virtualization $VER amd64 ($(date +%F))" > "$ISO/.disk/info"

echo "== 5/6 Boot menu"
cat > "$ISO/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=10
insmod all_video
insmod gfxterm
menuentry "Install Katam Virtualization $VER" {
  linux /casper/vmlinuz boot=casper quiet ---
  initrd /casper/initrd
}
menuentry "Install Katam Virtualization (safe graphics)" {
  linux /casper/vmlinuz boot=casper nomodeset ---
  initrd /casper/initrd
}
menuentry "Install with serial console (BMC Serial-over-LAN, ttyS0 115200)" {
  linux /casper/vmlinuz boot=casper console=tty0 console=ttyS0,115200n8 ---
  initrd /casper/initrd
}
menuentry "Boot from the first hard disk" {
  insmod chain
  set root=(hd1)
  chainloader +1
}
EOF

echo "== 6/6 Writing ISO"
rm -f "$OUT"
grub-mkrescue -o "$OUT" "$ISO" -- -volid "KATAM_${VER//./_}" 2>&1 | grep -v "^xorriso : UPDATE" || true
[[ -s $OUT ]] || { echo "ISO was not written" >&2; exit 1; }
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
echo
echo "Built $OUT ($(du -h "$OUT" | cut -f1)), kernel $KVER"
echo "USB:  sudo dd if=$(basename "$OUT") of=/dev/sdX bs=4M status=progress oflag=sync"
echo "BMC:  mount it as virtual media and boot from it once."
