#!/usr/bin/env bash
# Build katam-controller, katam-node and katam-witness .deb packages into ./dist
set -euo pipefail
VER=${VER:-0.5.1}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/packaging/dist"
B=$(mktemp -d)
rm -rf "$OUT"; mkdir -p "$OUT"

# ---------------- katam-controller ----------------
P=$B/katam-controller
install -d $P/DEBIAN $P/usr/lib/katam $P/usr/bin $P/lib/systemd/system $P/usr/share/katam $P/usr/share/doc/katam-controller
cp -r "$ROOT/controller/katam" $P/usr/lib/katam/
sed -i "s/^__version__ = .*/__version__ = \"$VER\"/" $P/usr/lib/katam/katam/__init__.py
find $P -name __pycache__ -prune -exec rm -rf {} +
cp "$ROOT/config.example.yaml" $P/usr/share/katam/
cp -r "$ROOT/examples" "$ROOT/integrations" $P/usr/share/katam/
cp "$ROOT/README.md" "$ROOT/ARCHITECTURE.md" $P/usr/share/doc/katam-controller/
cat > $P/usr/bin/katamctl <<'EOF'
#!/bin/sh
PYTHONPATH=/usr/lib/katam exec python3 -m katam.cli "$@"
EOF
chmod 755 $P/usr/bin/katamctl
cat > $P/usr/lib/katam/katam-vip <<'EOF'
#!/usr/bin/python3
# Adds or removes the control plane's virtual IP. Called by the leader (katam.service).
import sys
sys.path.insert(0, "/usr/lib/katam")
from katam.leader import vip_helper_main
vip_helper_main()
EOF
chmod 755 $P/usr/lib/katam/katam-vip
install -m 755 "$ROOT/packaging/katam-self-upgrade" $P/usr/lib/katam/katam-self-upgrade
# One-click upgrade of this controller: the service (unprivileged) drops a request file;
# this root-owned path unit notices it and runs katam-self-upgrade.
cat > $P/lib/systemd/system/katam-upgrade.path <<'EOF'
[Unit]
Description=Watch for Katam upgrade requests from the web UI

[Path]
PathExists=/var/lib/katam/upgrade/request
MakeDirectory=yes

[Install]
WantedBy=multi-user.target
EOF
cat > $P/lib/systemd/system/katam-upgrade.service <<'EOF'
[Unit]
Description=Upgrade Katam packages on this machine and restart Katam
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/lib/katam/katam-self-upgrade
TimeoutStartSec=1800
EOF
cat > $P/lib/systemd/system/katam.service <<'EOF'
[Unit]
Description=Katam Virtualization control plane
After=network-online.target postgresql.service etcd.service patroni.service
Wants=network-online.target

[Service]
User=katam
Group=katam
Environment=KATAM_CONFIG=/etc/katam/config.yaml
Environment=PYTHONPATH=/usr/lib/katam
ExecStart=/usr/bin/python3 -m katam
Restart=on-failure
RestartSec=3
# On SIGTERM the leader releases leadership at once, so another controller takes over.
TimeoutStopSec=20
# Only for the virtual IP (cluster.vip): add/remove an address and announce it with ARP.
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/katam
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
cat > $P/lib/systemd/system/katam-console.service <<'EOF'
[Unit]
Description=Katam VM consoles (websockify + noVNC)
After=network-online.target

[Service]
User=katam
Group=katam
ExecStart=/usr/bin/websockify --web /usr/share/novnc --token-plugin TokenFile --token-source /var/lib/katam/console-tokens 6080
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
cat > $P/DEBIAN/control <<EOF
Package: katam-controller
Version: $VER
Architecture: all
Maintainer: Katam Virtualization <admin@katam.local>
Depends: python3 (>= 3.10), python3-flask, python3-waitress, python3-yaml, python3-sqlalchemy, python3-libvirt, python3-pycdlib, python3-ldap3, python3-onelogin-saml2, python3-requests, python3-boto3, python3-lxml, python3-xmlsec, libvirt-clients, novnc, python3-websockify, openssh-client, qemu-utils, openssl, git, iproute2
Recommends: python3-psycopg2, iputils-arping, etcd-server, etcd-client, patroni, postgresql
Section: admin
Priority: optional
Homepage: https://katam.local
Description: Katam Virtualization control plane
 Web UI, REST API and CLI for KVM clusters: VMs, images, OVA import/export,
 RBAC with AD and SAML, SDN with OVN, HA with fencing, DR, runbooks and FinOps.
 Controllers can run on every node with etcd leader election and replicated
 PostgreSQL; Prometheus metrics, Grafana dashboards and runbooks kept in Git.
EOF
echo "/etc/katam/config.yaml" > $P/DEBIAN/conffiles.unused
rm $P/DEBIAN/conffiles.unused
cat > $P/DEBIAN/postinst <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  id katam >/dev/null 2>&1 || useradd -r -m -d /var/lib/katam -s /usr/sbin/nologin katam
  install -d -o katam -g katam -m 750 /var/lib/katam /var/lib/katam/console-tokens /var/lib/katam/staging /var/lib/katam/exports /var/lib/katam/.ssh /var/lib/katam/gitops
  [ -f /var/lib/katam/.ssh/id_ed25519 ] || su -s /bin/sh katam -c "ssh-keygen -q -t ed25519 -N '' -C katam@controller -f /var/lib/katam/.ssh/id_ed25519"
  install -d -o katam -g katam -m 750 /var/lib/katam/upgrade
  install -d -m 755 /etc/katam
  if [ ! -f /etc/katam/config.yaml ]; then
    SECRET=$(openssl rand -hex 32)
    PASS=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)
    sed -e "s/REPLACE_WITH_openssl_rand_hex_32/$SECRET/" \
        -e "s/CHANGE-ME-on-first-login/$PASS/" \
        -e "s/witness_token: REPLACE_WITH_RANDOM/witness_token: $(openssl rand -hex 16)/" \
        /usr/share/katam/config.example.yaml > /etc/katam/config.yaml
    chown root:katam /etc/katam/config.yaml; chmod 640 /etc/katam/config.yaml
    umask 077
    printf 'Katam first sign-in\n  user: admin\n  password: %s\nChange it under Access after signing in.\n' "$PASS" > /root/katam-initial-admin.txt
    echo "Katam: wrote /etc/katam/config.yaml. Admin password is in /root/katam-initial-admin.txt"
  fi
  systemctl daemon-reload || true
  systemctl enable katam katam-console >/dev/null 2>&1 || true
  systemctl enable --now katam-upgrade.path >/dev/null 2>&1 || true
  echo "Katam: edit /etc/katam/config.yaml (nodes, storage, networks), then: systemctl restart katam katam-console"
  echo "Katam: controller SSH key for prepare-node: $(cat /var/lib/katam/.ssh/id_ed25519.pub)"
fi
EOF
cat > $P/DEBIAN/prerm <<'EOF'
#!/bin/sh
set -e
[ "$1" = remove ] && systemctl stop katam katam-console 2>/dev/null || true
exit 0
EOF
chmod 755 $P/DEBIAN/postinst $P/DEBIAN/prerm

# ---------------- katam-node ----------------
N=$B/katam-node
install -d $N/DEBIAN $N/usr/share/katam-node $N/usr/sbin
cp "$ROOT"/node-setup/* $N/usr/share/katam-node/
chmod 755 $N/usr/share/katam-node/*.sh $N/usr/share/katam-node/katam-cluster
ln -s /usr/share/katam-node/prepare-node.sh $N/usr/sbin/katam-node-setup
ln -s /usr/share/katam-node/katam-cluster $N/usr/sbin/katam-cluster
install -d -m 755 $N/etc/sudoers.d
printf '# One-click upgrades: the controller restarts Katam on this node after upgrading it.\nkatam ALL=(root) NOPASSWD: /usr/bin/systemctl restart katam, /usr/bin/systemctl restart katam-console\n' > $N/etc/sudoers.d/katam-upgrade
chmod 440 $N/etc/sudoers.d/katam-upgrade
cat > $N/DEBIAN/control <<EOF
Package: katam-node
Version: $VER
Architecture: all
Maintainer: Katam Virtualization <admin@katam.local>
Depends: bash, sudo, openssh-server
Recommends: qemu-system-x86, libvirt-daemon-system, ceph-common, etcd-server, etcd-client, patroni, postgresql
Section: admin
Priority: optional
Description: Katam Virtualization node setup tools
 katam-node-setup prepares any x86-64 Ubuntu 24.04 server as a KVM node
 (Intel or AMD). katam-cluster runs the control plane on the nodes (etcd leader
 election, Patroni PostgreSQL). Also ships Ceph, OVN, RBD mirroring and
 stretch-cluster scripts in /usr/share/katam-node.
EOF

# ---------------- katam-witness ----------------
W=$B/katam-witness
install -d $W/DEBIAN $W/usr/lib/katam-witness $W/lib/systemd/system $W/etc/default
cp "$ROOT/controller/katam/witness.py" $W/usr/lib/katam-witness/katam_witness.py
install -m 755 "$ROOT/node-setup/katam-cluster" $W/usr/lib/katam-witness/katam-cluster
cat > $W/lib/systemd/system/katam-witness.service <<'EOF'
[Unit]
Description=Katam witness (second opinion for HA fencing)
After=network-online.target

[Service]
EnvironmentFile=/etc/default/katam-witness
DynamicUser=yes
ExecStart=/usr/bin/python3 /usr/lib/katam-witness/katam_witness.py --port ${KATAM_WITNESS_PORT}
Restart=always

[Install]
WantedBy=multi-user.target
EOF
cat > $W/etc/default/katam-witness <<'EOF'
# Same value as ha.witness_token in the controller's config.yaml
KATAM_WITNESS_TOKEN=
KATAM_WITNESS_PORT=8443
EOF
echo /etc/default/katam-witness > $W/DEBIAN/conffiles
cat > $W/DEBIAN/control <<EOF
Package: katam-witness
Version: $VER
Architecture: all
Maintainer: Katam Virtualization <admin@katam.local>
Depends: python3, python3-flask, python3-waitress, openssl
Recommends: etcd-server, etcd-client
Section: admin
Priority: optional
Description: Katam Virtualization witness
 Small probe service for a third location. The controller fences a node only
 when the witness also can't reach it, and a DR site fails over only when the
 witness confirms the primary is down. With etcd it is the tie-breaker vote for
 two-node clusters (/usr/lib/katam-witness/katam-cluster join ...).
EOF
cat > $W/DEBIAN/postinst <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  if ! grep -q '^KATAM_WITNESS_TOKEN=.' /etc/default/katam-witness; then
    sed -i "s/^KATAM_WITNESS_TOKEN=.*/KATAM_WITNESS_TOKEN=$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')/" /etc/default/katam-witness
    echo "Katam witness: token generated in /etc/default/katam-witness; copy it to ha.witness_token on the controller."
  fi
  systemctl daemon-reload || true
  systemctl enable --now katam-witness >/dev/null 2>&1 || true
fi
EOF
chmod 755 $W/DEBIAN/postinst

# Update channel: if release/repo.conf (KATAM_APT_URL=..., KATAM_APT_CHANNEL=stable) and
# release/katam-archive-keyring.gpg exist, build katam-archive-keyring (apt source + key)
# and make the other packages depend on it, so installed servers get new versions with apt.
PKGS="katam-controller katam-node katam-witness"
if [[ -f "$ROOT/release/repo.conf" && -f "$ROOT/release/katam-archive-keyring.gpg" ]]; then
  # shellcheck disable=SC1091
  . "$ROOT/release/repo.conf"
  K=$B/katam-archive-keyring
  install -d "$K/DEBIAN" "$K/usr/share/keyrings" "$K/etc/apt/sources.list.d"
  cp "$ROOT/release/katam-archive-keyring.gpg" "$K/usr/share/keyrings/"
  printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: main\nSigned-By: /usr/share/keyrings/katam-archive-keyring.gpg\n' \
    "$KATAM_APT_URL" "${KATAM_APT_CHANNEL:-stable}" > "$K/etc/apt/sources.list.d/katam.sources"
  echo /etc/apt/sources.list.d/katam.sources > "$K/DEBIAN/conffiles"
  printf 'Package: katam-archive-keyring\nVersion: %s\nArchitecture: all\nMaintainer: Katam Virtualization <admin@katam.local>\nSection: admin\nPriority: optional\nDescription: Katam Virtualization update channel\n apt source and signing key for Katam package updates.\n' "$VER" > "$K/DEBIAN/control"
  for c in "$P" "$N" "$W"; do sed -i 's/^Depends: \(.*\)$/Depends: \1, katam-archive-keyring/' "$c/DEBIAN/control"; done
  PKGS="$PKGS katam-archive-keyring"
  echo "${KATAM_APT_URL%/apt}/releases.json" > "$P/usr/share/katam/update-feed"
  echo "update channel: $KATAM_APT_URL (${KATAM_APT_CHANNEL:-stable})"
fi

for pkg in $PKGS; do
  dpkg-deb --root-owner-group --build "$B/$pkg" "$OUT/${pkg}_${VER}_all.deb" >/dev/null
done
rm -rf "$B"
ls -l "$OUT"
