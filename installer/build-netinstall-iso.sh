#!/usr/bin/env bash
# Build the small Katam network-install ISO (under 250 MB).
#
# It carries a minimal live system, the installer and the Katam packages. During
# installation it downloads Ubuntu 24.04 and the virtualization packages from the Ubuntu
# archive (or your mirror/proxy), so servers need internet access while installing.
# For fully offline installs use build-offline-iso.sh instead (about 620 MB).
#
#   sudo apt install debootstrap squashfs-tools xorriso grub-pc-bin grub-efi-amd64-bin mtools dosfstools
#   sudo installer/build-netinstall-iso.sh
set -euo pipefail

VER=${VER:-0.5.0}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
WORK=${WORK:-$HERE/build-net}
MIRROR=${MIRROR:-http://archive.ubuntu.com/ubuntu}
OUT=${OUT:-$HERE/katam-virtualization-$VER-netinstall-amd64.iso}
C=$WORK/root
ISO=$WORK/iso
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }

PKGS=(linux-image-generic casper systemd-sysv systemd-resolved udev kmod whiptail gdisk parted
      dosfstools e2fsprogs debootstrap ubuntu-keyring gpgv curl ca-certificates iproute2 iputils-ping
      pciutils ethtool less nano kbd)

mkdir -p "$WORK"
cleanup() { for m in dev/pts dev proc sys run; do umount -lf "$C/$m" 2>/dev/null || true; done; }
trap cleanup EXIT

echo "== 1/5 Minimal base system"
[[ -x $C/usr/bin/apt-get ]] || debootstrap --arch=amd64 --variant=minbase --components=main,universe noble "$C" "$MIRROR"

echo "== 2/5 Live system packages"
printf 'deb %s noble main universe\ndeb %s noble-updates main universe\ndeb %s noble-security main universe\n' \
  "$MIRROR" "$MIRROR" "$MIRROR" > "$C/etc/apt/sources.list"
for m in dev dev/pts proc sys run; do mkdir -p "$C/$m"; mount --bind "/$m" "$C/$m"; done
rm -f "$C/etc/resolv.conf"; cp -L /etc/resolv.conf "$C/etc/resolv.conf"
PROXY=${http_proxy:-${HTTP_PROXY:-}}
[[ -n $PROXY ]] && printf 'Acquire::http::Proxy "%s";\n' "$PROXY" > "$C/etc/apt/apt.conf.d/99build-proxy"
printf '#!/bin/sh\nexit 101\n' > "$C/usr/sbin/policy-rc.d"; chmod +x "$C/usr/sbin/policy-rc.d"
chroot "$C" bash -c "set -e; export DEBIAN_FRONTEND=noninteractive LANG=C
  apt-get update -q
  apt-get install -y -q --no-install-recommends ${PKGS[*]}
  # Restore the full kernel, drivers and firmware (a previous build trims them) so the
  # initrd always contains every storage/network driver.
  apt-get install -y -q --reinstall --no-install-recommends \$(dpkg-query -W -f='\${Package}\n' 'linux-image-[0-9]*' 'linux-modules-[0-9]*' 'linux-modules-extra-[0-9]*' linux-firmware 2>/dev/null)
  apt-get clean"
rm -f "$C/etc/apt/apt.conf.d/99build-proxy" "$C/usr/sbin/policy-rc.d"

echo "== 3/5 Installer and Katam packages"
[[ -f $ROOT/packaging/dist/katam-controller_${VER}_all.deb ]] || "$ROOT/packaging/build-debs.sh"
rm -rf "$C/opt/katam-debs"; mkdir -p "$C/opt/katam-debs"
cp "$ROOT"/packaging/dist/katam-*_${VER}_all.deb "$C/opt/katam-debs/"
sed "s/^VER=.*/VER=$VER/" "$HERE/iso/katam-install" > "$C/usr/local/sbin/katam-install"
chmod 755 "$C/usr/local/sbin/katam-install"
echo "Katam Virtualization $VER network installer" > "$C/etc/katam-live"
touch "$C/etc/katam-netinstall"
echo "katam-installer" > "$C/etc/hostname"
mkdir -p "$C/etc/systemd/system/getty@tty1.service.d" "$C/etc/systemd/network"
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %%I $TERM\n' \
  > "$C/etc/systemd/system/getty@tty1.service.d/zz-katam-live.conf"
cat > "$C/root/.bash_profile" <<'EOF'
if [ -f /etc/katam-live ] && [ "$(tty)" = /dev/tty1 ] && [ -z "${KATAM_SHELL:-}" ]; then
  /usr/local/sbin/katam-install
  echo; echo "Installer exited. Type katam-install to start again."
fi
EOF
printf '[Match]\nName=e*\n[Network]\nDHCP=ipv4\n' > "$C/etc/systemd/network/99-dhcp-all.network"
chroot "$C" systemctl enable systemd-networkd systemd-resolved >/dev/null 2>&1 || true
: > "$C/etc/machine-id"
chroot "$C" update-initramfs -u -k all

echo "== 4/5 Trimming and compressing"
rm -rf "$ISO"; mkdir -p "$ISO/casper" "$ISO/boot/grub" "$ISO/.disk"
KVER=$(ls "$C/boot" | sed -n 's/^vmlinuz-//p' | sort -V | tail -1)
cp "$C/boot/vmlinuz-$KVER" "$ISO/casper/vmlinuz"
cp "$C/boot/initrd.img-$KVER" "$ISO/casper/initrd"
cleanup
rm -f "$C/etc/resolv.conf"; ln -s ../run/systemd/resolve/stub-resolv.conf "$C/etc/resolv.conf"
# The initrd already holds every storage/network driver and its firmware. The live system
# keeps only what it needs after boot: networking modules (VLANs, bonding) and server NIC firmware.
"$HERE/slim-image.sh" "$C"
# Driver trees the live system never needs after boot are left out of the image.
EXCL=(boot)
for d in drivers/gpu drivers/media drivers/net/wireless drivers/staging drivers/iio drivers/infiniband \
         drivers/bluetooth drivers/video drivers/sound sound net/wireless net/mac80211 net/bluetooth \
         fs/ocfs2 fs/btrfs fs/xfs fs/f2fs; do
  EXCL+=("usr/lib/modules/$KVER/kernel/$d")
done
rm -rf "$C"/tmp/*
mksquashfs "$C" "$ISO/casper/filesystem.squashfs" -noappend -comp xz -b 1M -e "${EXCL[@]}"
du -sx --block-size=1 "$C" | cut -f1 > "$ISO/casper/filesystem.size"
echo "Katam Virtualization $VER network installer amd64 ($(date +%F))" > "$ISO/.disk/info"

echo "== 5/5 Boot menu and ISO"
cat > "$ISO/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=10
insmod all_video
menuentry "Install Katam Virtualization $VER (downloads packages)" {
  linux /casper/vmlinuz boot=casper quiet ---
  initrd /casper/initrd
}
menuentry "Install (safe graphics)" {
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
rm -f "$OUT"
grub-mkrescue -o "$OUT" "$ISO" -- -volid "KATAM_NET_${VER//./_}" 2>&1 | grep -v "^xorriso : UPDATE" || true
[[ -s $OUT ]] || { echo "ISO was not written" >&2; exit 1; }
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
echo
echo "Built $OUT ($(du -h "$OUT" | cut -f1)), kernel $KVER"
