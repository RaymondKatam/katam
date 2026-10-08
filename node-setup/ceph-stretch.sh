#!/usr/bin/env bash
# Zero-RPO metro DR: one Ceph cluster stretched across two sites (<5 ms round trip)
# with a tie-breaker monitor on the witness host at a third location.
#
#   sudo ./ceph-stretch.sh siteA "node1 node2" siteB "node3 node4" witness-host
#
# Needs 5 monitors (2 per site + 1 on the witness) and equal OSD capacity per site.
set -euo pipefail
A=$1; A_HOSTS=$2; B=$3; B_HOSTS=$4; WITNESS=$5
ceph osd crush add-bucket "$A" datacenter; ceph osd crush move "$A" root=default
ceph osd crush add-bucket "$B" datacenter; ceph osd crush move "$B" root=default
for h in $A_HOSTS; do ceph osd crush move "$h" datacenter="$A"; ceph mon set_location "$h" datacenter="$A"; done
for h in $B_HOSTS; do ceph osd crush move "$h" datacenter="$B"; ceph mon set_location "$h" datacenter="$B"; done
ceph orch host add "$WITNESS" --labels mon
ceph orch apply mon --placement="$(echo $A_HOSTS $B_HOSTS $WITNESS | tr ' ' ',')"
sleep 30
ceph mon set_location "$WITNESS" datacenter=witness
ceph mon set election_strategy connectivity
cat > /tmp/stretch-rule.txt <<RULE
rule stretch_rule {
  id 111
  type replicated
  step take $A
  step chooseleaf firstn 2 type host
  step emit
  step take $B
  step chooseleaf firstn 2 type host
  step emit
}
RULE
ceph osd getcrushmap -o /tmp/crush.bin
crushtool -d /tmp/crush.bin -o /tmp/crush.txt
grep -q stretch_rule /tmp/crush.txt || cat /tmp/stretch-rule.txt >> /tmp/crush.txt
crushtool -c /tmp/crush.txt -o /tmp/crush.new && ceph osd setcrushmap -i /tmp/crush.new
ceph mon enable_stretch_mode "$WITNESS" stretch_rule datacenter
echo "Stretch mode on: 4 copies, 2 per site; writes acknowledge at both sites (RPO 0)."
