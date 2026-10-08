#!/usr/bin/env bash
# Run the OVN databases and northd on one node (the sdn.central_node in config.yaml).
#   sudo ./ovn-central.sh 172.20.10.11
set -euo pipefail
IP=$1
apt-get install -y ovn-central
systemctl enable --now ovn-central
ovn-nbctl set-connection ptcp:6641:"$IP"
ovn-sbctl set-connection ptcp:6642:"$IP"
echo "OVN central listening on $IP (6641 northbound, 6642 southbound)."
echo "For three-node OVN database HA, see the install guide's SDN chapter."
