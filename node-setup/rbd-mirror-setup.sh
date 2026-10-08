#!/usr/bin/env bash
# Asynchronous DR: snapshot mirroring of the VM pool from the primary Ceph cluster to a
# separate Ceph cluster at the DR site. Run step 1 on a primary node, step 2 on a DR node.
#
#   primary$ sudo ./rbd-mirror-setup.sh primary vms
#            (prints a bootstrap token; copy it to the DR site)
#   dr$      sudo ./rbd-mirror-setup.sh dr vms <token-file>
set -euo pipefail
ROLE=$1; POOL=$2
ceph orch apply rbd-mirror --placement=1
rbd mirror pool enable "$POOL" image
if [[ $ROLE == primary ]]; then
  rbd mirror pool peer bootstrap create --site-name primary "$POOL" > /root/rbd-mirror-token
  echo "Token written to /root/rbd-mirror-token. Copy it to a DR node."
else
  rbd mirror pool peer bootstrap import --site-name dr --direction rx-tx "$POOL" "$3"
  echo "Peering done. Check with: rbd mirror pool status $POOL --verbose"
  echo "Katam enables mirroring per VM disk when you save a DR plan."
fi
