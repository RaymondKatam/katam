#!/usr/bin/env bash
# Build a hyperconverged Ceph cluster with cephadm (3+ nodes). Run once, on the first node.
#
#   sudo ./ceph-bootstrap.sh 172.20.30.11 172.20.30.0/24 node2:172.20.30.12 node3:172.20.30.13
#
# Host names must match each node's `hostname`. Disks for Ceph must be empty and visible
# as raw disks (RAID controller in HBA/JBOD/pass-through mode).
set -euo pipefail
MON_IP=$1; CLUSTER_NET=$2; shift 2
CEPH_POOL=${CEPH_POOL:-vms}
K8S_POOL=${K8S_POOL:-kubernetes}

apt-get install -y cephadm
cephadm bootstrap --mon-ip "$MON_IP" --cluster-network "$CLUSTER_NET" --skip-monitoring-stack

echo
echo "Add this key to /root/.ssh/authorized_keys on every other node:"
cat /etc/ceph/ceph.pub
read -rp "Press Enter once that's done... "

for h in "$@"; do
  ceph orch host add "${h%%:*}" "${h##*:}" --labels _admin
done

ceph orch apply mon 3
ceph orch apply osd --all-available-devices
for p in "$CEPH_POOL" "$K8S_POOL"; do
  ceph osd pool create "$p" || true
  ceph osd pool set "$p" size 3
  ceph osd pool set "$p" min_size 2
  rbd pool init "$p"
done
ceph config set osd osd_memory_target 4294967296

ceph auth get-or-create client.libvirt mon 'profile rbd' osd "profile rbd pool=$CEPH_POOL" >/dev/null
ceph auth get-or-create client.kubernetes mon 'profile rbd' osd "profile rbd pool=$K8S_POOL" mgr "profile rbd pool=$K8S_POOL" >/dev/null
UUID=$(uuidgen)
cat <<MSG

Run on EVERY node:
  sudo ./rbd-pool.sh '$(ceph auth get-key client.libvirt)' $UUID $MON_IP,<mon2>,<mon3>

Put this in /etc/katam/config.yaml:
storage:
  type: rbd
  pool: vmpool
  rbd:
    ceph_pool: $CEPH_POOL
    user: libvirt
    secret_uuid: $UUID
    monitors: [$MON_IP, <mon2>, <mon3>]
    fsid: $(ceph fsid)
    csi: {user: kubernetes, key: $(ceph auth get-key client.kubernetes), pool: $K8S_POOL}

Watch OSDs come up with: ceph -s
MSG
