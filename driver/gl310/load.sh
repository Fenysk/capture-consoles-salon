#!/bin/sh
# Développement : charge le gl310.ko fraîchement compilé (sans DKMS), signé avec la clé MOK d'Ubuntu.
#   sudo driver/gl310/load.sh          recharge le module
#   sudo driver/gl310/load.sh --unload retire le module (les outils Python peuvent de nouveau parler au boîtier)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
MOK_KEY=/var/lib/shim-signed/mok/MOK.priv
MOK_CERT=/var/lib/shim-signed/mok/MOK.der

[ "$(id -u)" = 0 ] || { echo "À lancer avec sudo : sudo $0 $*"; exit 1; }

if lsmod | grep -q '^gl310 '; then
	rmmod gl310
	echo "Ancien module gl310 retiré."
fi
[ "${1:-}" = "--unload" ] && exit 0

[ -f "$HERE/gl310.ko" ] || { echo "Compile d'abord : make -C $HERE"; exit 1; }
for fw in qpvidfwusb.bin qpaudfwusb.bin; do
	install -D -m 0644 "$ROOT/vendor/gl310-driver/extracted/$fw" "/lib/firmware/avermedia/$fw"
done

SIGNED=$(mktemp --suffix=.ko)
trap 'rm -f "$SIGNED"' EXIT
cp "$HERE/gl310.ko" "$SIGNED"
if mokutil --sb-state 2>/dev/null | grep -q enabled; then
	kmodsign sha512 "$MOK_KEY" "$MOK_CERT" "$SIGNED"
fi
modprobe videodev
insmod "$SIGNED"
echo "Module gl310 chargé. Messages du pilote :"
sleep 1
dmesg | grep -E 'gl310' | tail -n 5
