#!/usr/bin/env bash
# Build a signed apt repository from .deb files.
#   release/publish-apt-repo.sh <debs-dir> <site-dir> [channel]
# Needs: apt-utils (apt-ftparchive), gnupg, and the signing key in the keyring
# (KATAM_GPG_KEY_ID selects it). Output layout under <site-dir>/apt:
#   dists/<channel>/main/binary-all/Packages(.gz), dists/<channel>/{Release,InRelease,Release.gpg}
#   pool/main/*.deb, katam-archive-keyring.gpg, katam-archive-keyring.asc
set -euo pipefail
DEBS=$1; SITE=$2; CHANNEL=${3:-stable}
KEY=${KATAM_GPG_KEY_ID:?set KATAM_GPG_KEY_ID to the signing key id or email}
R=$SITE/apt
mkdir -p "$R/pool/main" "$R/dists/$CHANNEL/main/binary-all" "$R/dists/$CHANNEL/main/binary-amd64"
cp "$DEBS"/*.deb "$R/pool/main/"
cd "$R"
apt-ftparchive packages pool/main > "dists/$CHANNEL/main/binary-all/Packages"
cp "dists/$CHANNEL/main/binary-all/Packages" "dists/$CHANNEL/main/binary-amd64/Packages"
for d in binary-all binary-amd64; do gzip -kf9 "dists/$CHANNEL/main/$d/Packages"; done
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=Katam \
  -o APT::FTPArchive::Release::Label="Katam Virtualization" \
  -o APT::FTPArchive::Release::Suite="$CHANNEL" \
  -o APT::FTPArchive::Release::Codename="$CHANNEL" \
  -o APT::FTPArchive::Release::Architectures="amd64 all" \
  -o APT::FTPArchive::Release::Components=main \
  release "dists/$CHANNEL" > "dists/$CHANNEL/Release"
gpg --batch --yes --default-key "$KEY" --clearsign -o "dists/$CHANNEL/InRelease" "dists/$CHANNEL/Release"
gpg --batch --yes --default-key "$KEY" -abs -o "dists/$CHANNEL/Release.gpg" "dists/$CHANNEL/Release"
gpg --export "$KEY" > katam-archive-keyring.gpg
gpg --armor --export "$KEY" > katam-archive-keyring.asc
echo "apt repository ready in $R ($CHANNEL)"
