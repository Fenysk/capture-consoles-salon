#!/bin/sh
# Installation durable : firmwares dans /lib/firmware, module géré par DKMS (recompilé et signé à chaque
# mise à jour du noyau), chargé automatiquement au branchement du boîtier.
#   sudo driver/gl310/install.sh             installe ou met à jour
#   sudo driver/gl310/install.sh --remove    désinstalle
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
VERSION=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "$HERE/dkms.conf")
SRC=/usr/src/gl310-$VERSION

[ "$(id -u)" = 0 ] || { echo "À lancer avec sudo : sudo $0 $*"; exit 1; }

lsmod | grep -q '^gl310 ' && rmmod gl310
dkms status gl310 2>/dev/null | grep -q gl310 && dkms remove "gl310/$VERSION" --all
rm -rf "$SRC"
if [ "${1:-}" = "--remove" ]; then
	rm -f /lib/firmware/avermedia/qpvidfwusb.bin /lib/firmware/avermedia/qpaudfwusb.bin
	echo "Pilote gl310 désinstallé."
	exit 0
fi

for fw in qpvidfwusb.bin qpaudfwusb.bin; do
	install -D -m 0644 "$ROOT/vendor/gl310-driver/extracted/$fw" "/lib/firmware/avermedia/$fw"
done
mkdir -p "$SRC"
cp "$HERE"/*.c "$HERE"/*.h "$HERE/Makefile" "$HERE/dkms.conf" "$SRC/"
dkms add "gl310/$VERSION"
dkms build "gl310/$VERSION"
dkms install "gl310/$VERSION"
modprobe gl310
echo "Pilote gl310 $VERSION installé : il se chargera tout seul au branchement du boîtier."
