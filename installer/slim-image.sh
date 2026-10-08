#!/usr/bin/env bash
# Shrink the installer's system image: keep firmware servers need, drop the rest.
#   installer/slim-image.sh <chroot>
# Removed: Wi-Fi, Bluetooth, laptop/desktop GPU, audio, camera, phone SoC and switch-ASIC
# firmware; package lists (apt update recreates them); manuals; non-English translations.
# Kept: server NIC/HBA firmware, CPU microcode, and every package's copyright file.
# The removed firmware returns on installed servers the next time linux-firmware is
# upgraded with apt, so nothing is lost for good.
set -euo pipefail
C=$1
FW=$C/usr/lib/firmware
[[ -d $FW ]] || { echo "no firmware dir in $C" >&2; exit 1; }
before=$(du -sm "$FW" | cut -f1)

# Top-level firmware entries to keep (server network and storage adapters, microcode).
KEEP=(bnx2 bnx2x qed qlogic ql2100_fw.bin ql2200_fw.bin ql2300_fw.bin ql2322_fw.bin ql2400_fw.bin
      ql2500_fw.bin cxgb3 cxgb4 tigon e100 rtl_nic intel-ucode amd-ucode amd intel
      myri10ge_ethp_z8e.dat myri10ge_eth_z8e.dat sxg slicoss tehuti vxge regulatory.db
      regulatory.db.p7s)
keep() { local n=$1; for k in "${KEEP[@]}"; do [[ $n == "$k" || $n == "$k".* ]] && return 0; done; return 1; }
for e in "$FW"/*; do
  n=$(basename "$e")
  keep "$n" || rm -rf "$e"
done
# In intel/, keep only Ethernet (ice/ixgbe/i40e packages); drop Wi-Fi, audio, camera, VPU.
if [[ -d $FW/intel ]]; then
  for e in "$FW"/intel/*; do
    case $(basename "$e") in ice*|ixgbe*|i40e*|irdma*) ;; *) rm -rf "$e" ;; esac
  done
fi
# In amd/, keep only microcode and SEV (secure VMs); drop GPU-related blobs.
if [[ -d $FW/amd ]]; then
  for e in "$FW"/amd/*; do
    case $(basename "$e") in amd_sev*|microcode*) ;; *) rm -rf "$e" ;; esac
  done
fi
after=$(du -sm "$FW" | cut -f1)

rm -rf "$C"/var/lib/apt/lists/* "$C"/var/cache/apt/*.bin
find "$C/usr/share/doc" -mindepth 1 \( -type f -o -type l \) ! -name copyright -delete 2>/dev/null || true
find "$C/usr/share/doc" -mindepth 1 -type d -empty -delete 2>/dev/null || true
rm -rf "$C"/usr/share/man/* "$C"/usr/share/info/* "$C"/usr/share/help/*
find "$C/usr/share/locale" -mindepth 1 -maxdepth 1 -type d ! -name 'en*' ! -name 'C*' -exec rm -rf {} + 2>/dev/null || true

echo "firmware: ${before} MB -> ${after} MB; docs, manuals, package lists and translations removed"
