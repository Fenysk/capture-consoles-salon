#!/bin/sh
# Télécharge dans vendor/ les sources tierces que le dépôt ne publie pas (licences d'AVerMedia et des auteurs) :
# pilote Windows du GL310 (firmwares qpaudfwusb.bin/qpvidfwusb.bin), trace USB du C875, pilote Linux du C985.
#
#   tools/fetch-vendor.sh    à lancer une fois avant ./install.sh ; ne refait pas ce qui est déjà là
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
V=$ROOT/vendor
mkdir -p "$V"

DRIVER=$V/gl310-driver
if [ ! -f "$DRIVER/extracted/qpvidfwusb.bin" ]; then
	echo "==> Pilote Windows du GL310 (archive.org, 1,9 Mo)"
	mkdir -p "$DRIVER/extracted"
	curl -fL -o "$DRIVER/gl310-driver.zip" \
		"https://archive.org/download/aver-media-gl-310-usb-hd-capture-device-v-3.2802.64.40/AVerMedia%20GL310%20USB%20HD%20Capture%20Device%20v3.2802.64.40.zip"
	echo "6aa363d7d7458dd08f021277353d6f2f  $DRIVER/gl310-driver.zip" | md5sum -c --quiet
	unzip -q -o -d "$DRIVER/extracted" "$DRIVER/gl310-driver.zip" \
		qpaudfwusb.bin qpvidfwusb.bin AVer835_x64.inf AVer330USB.sys aver330usb.cat
fi

clone() { # clone <dossier> <url> <commit>
	[ -d "$V/$1/.git" ] && return
	echo "==> $2"
	git clone -q "$2" "$V/$1"
	git -C "$V/$1" checkout -q "$3"
}
clone octv_gears_lgp https://github.com/Trouffman/octv_gears_lgp.git 2079baba56b0a6e421438edafb8d3e0661590bc7
clone avermedia-c985-linux https://github.com/FireCulex/avermedia-c985-linux.git 0782e4633775f171710bf1072d755b0ec5021ada

echo "vendor/ prêt."
