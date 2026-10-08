#!/bin/sh
# One-time switch to one-click upgrades for a server installed before the update channel existed.
#   curl -fsSL https://raymondkatam.github.io/katam/enable-updates.sh | sudo sh
# Adds the signed Katam apt repository, upgrades the Katam packages already installed here, and
# points the controller (if any) at the release feed. After this, upgrades are one click in the UI.
set -eu
BASE="${KATAM_UPDATES_BASE:-https://raymondkatam.github.io/katam}"
CHANNEL="${KATAM_CHANNEL:-stable}"
[ "$(id -u)" = 0 ] || { echo "Run as root: curl -fsSL $BASE/enable-updates.sh | sudo sh"; exit 1; }
echo "Katam: adding the update channel ($BASE/apt, $CHANNEL)"
TMP=$(mktemp)
curl -fsSL "$BASE/apt/katam-archive-keyring.gpg" -o "$TMP"
install -D -m 644 "$TMP" /usr/share/keyrings/katam-archive-keyring.gpg
rm -f "$TMP"
printf 'Types: deb\nURIs: %s/apt\nSuites: %s\nComponents: main\nSigned-By: /usr/share/keyrings/katam-archive-keyring.gpg\n' \
  "$BASE" "$CHANNEL" > /etc/apt/sources.list.d/katam.sources
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
PKGS="katam-archive-keyring"
for p in katam-controller katam-node katam-witness; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" && PKGS="$PKGS $p"
done
# shellcheck disable=SC2086
apt-get install -y -q -o Dpkg::Options::=--force-confold $PKGS
if [ -f /etc/katam/config.yaml ]; then
  python3 - "$BASE/releases.json" <<'PY'
import re, sys
url, p = sys.argv[1], "/etc/katam/config.yaml"
s = open(p).read()
line = re.compile(r"^(\s+)feed_url:\s*(null|~)?\s*(#.*)?$", re.M)
if re.search(r"^\s+feed_url:\s*\S*https?://", s, re.M):
    print("Katam: update feed already set")
elif line.search(s):
    s = line.sub(lambda m: f"{m.group(1)}feed_url: {url}", s, count=1)
    open(p, "w").write(s)
    print(f"Katam: update feed set to {url}")
elif not re.search(r"^updates:", s, re.M):
    open(p, "a").write(f"\nupdates:\n  feed_url: {url}\n  channel: stable\n")
    print(f"Katam: update feed set to {url}")
else:
    print(f"Katam: set updates.feed_url: {url} in {p} by hand")
PY
  systemctl restart katam katam-console 2>/dev/null || true
fi
echo "Katam: done. Installed: $(dpkg-query -W -f='${Package} ${Version}\n' 'katam-*' 2>/dev/null | tr '\n' ' ')"
echo "Next releases show an 'Upgrade now' banner in the Katam UI."
