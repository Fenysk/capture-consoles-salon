#!/usr/bin/env bash
# Phase 1 : outils de capture + accès au LGP Lite (GL310) sans root.
set -euo pipefail

RULE=/etc/udev/rules.d/70-avermedia-gl310.rules

echo "==> Installation des paquets"
sudo apt-get update
sudo apt-get install -y python3-usb libusb-1.0-0-dev ffmpeg v4l-utils

echo "==> Règle udev ($RULE)"
# c835 = boîtier au branchement, d835 = identifiant probable après chargement du firmware.
sudo tee "$RULE" >/dev/null <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="07ca", ATTR{idProduct}=="c835", MODE="0660", GROUP="plugdev", TAG+="uaccess"
SUBSYSTEM=="usb", ATTR{idVendor}=="07ca", ATTR{idProduct}=="d835", MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=07ca

echo "==> Vérification"
lsusb -d 07ca:
for dev in /sys/bus/usb/devices/*; do
  if [ "$(cat "$dev/idVendor" 2>/dev/null)" = "07ca" ]; then
    ls -l "/dev/bus/usb/$(printf '%03d' "$(cat "$dev/busnum")")/$(printf '%03d' "$(cat "$dev/devnum")")"
  fi
done
echo "==> Terminé"
