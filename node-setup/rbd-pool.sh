#!/usr/bin/env bash
# Register the Ceph pool with libvirt on this node. Run on every node.
#   sudo ./rbd-pool.sh <client.libvirt key> <secret uuid> <mon1,mon2,mon3>
set -euo pipefail
KEY=$1; UUID=$2; MONS=$3
CEPH_POOL=${CEPH_POOL:-vms}; POOL=${LIBVIRT_POOL:-vmpool}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/secret.xml" <<XML
<secret ephemeral='no' private='yes'>
  <uuid>$UUID</uuid>
  <usage type='ceph'><name>client.libvirt secret</name></usage>
</secret>
XML
virsh secret-define "$TMP/secret.xml"
printf '%s' "$KEY" > "$TMP/key"
virsh secret-set-value --secret "$UUID" --file "$TMP/key"

HOSTS=""
IFS=, read -ra M <<< "$MONS"
for m in "${M[@]}"; do HOSTS+="<host name='$m' port='6789'/>"; done
cat > "$TMP/pool.xml" <<XML
<pool type='rbd'>
  <name>$POOL</name>
  <source>
    <name>$CEPH_POOL</name>
    $HOSTS
    <auth username='libvirt' type='ceph'><secret uuid='$UUID'/></auth>
  </source>
</pool>
XML
virsh pool-info "$POOL" >/dev/null 2>&1 || virsh pool-define "$TMP/pool.xml"
virsh pool-autostart "$POOL"
virsh pool-info "$POOL" | grep -q running || virsh pool-start "$POOL"
virsh pool-info "$POOL"
