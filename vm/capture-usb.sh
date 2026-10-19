#!/usr/bin/env bash
# Enregistre le trafic USB du bus où se trouve le LGP Lite (usbmon), pendant que la VM utilise le pilote Windows.
# À lancer AVANT vm/start-vm.sh --usb, boîtier fraîchement rebranché. Ctrl-C pour arrêter.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$DIR/../logs"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/win-trace-$(date +%Y%m%d-%H%M%S).pcapng"

LINE="$(lsusb -d 07ca:c835 || true)"
[ -n "$LINE" ] || { echo "LGP Lite (07ca:c835) introuvable : branche-le d'abord."; exit 1; }
BUS=$((10#$(echo "$LINE" | awk '{print $2}')))
DEVNUM=$((10#$(echo "$LINE" | awk '{print $4}' | tr -d ':')))

sudo modprobe usbmon
echo "Capture de usbmon$BUS (LGP Lite = périphérique $DEVNUM) vers $OUT"
echo "Ctrl-C pour arrêter."
sudo dumpcap -q -i "usbmon$BUS" -w "$OUT"
sudo chown "$(id -u):$(id -g)" "$OUT"
echo "Capture enregistrée : $OUT"
