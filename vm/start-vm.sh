#!/usr/bin/env bash
# VM Windows 11 de laboratoire : QEMU/KVM, UEFI avec Secure Boot, TPM 2.0 émulé (swtpm).
# Sert à faire tourner le pilote officiel du LGP Lite (GL310) pendant que Linux enregistre le trafic USB.
#
# Usage :
#   vm/start-vm.sh [--usb] [--net]
#     --usb   passe le LGP Lite (07ca:c835) à la VM
#     --net   donne un accès réseau à la VM (par défaut : aucun réseau, inutile ici)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ISO="$DIR/iso/win11-ltsc-eval-en-us.iso"
DISK="$DIR/win11.qcow2"
VARS="$DIR/OVMF_VARS.fd"
TPM_DIR="$DIR/tpm"
ID_FILE="$DIR/vm-identity"
# Socket du TPM : chemin court (limite de 108 caractères) et autorisé par AppArmor (/tmp).
TPM_SOCK="/tmp/win11-lab-swtpm-$(id -u).sock"
# Console QEMU (sendkey, screendump…) : vm/vmctl.py s'y connecte.
MON_SOCK="/tmp/win11-lab-monitor-$(id -u).sock"
OVMF_CODE=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd
OVMF_VARS_TEMPLATE=/usr/share/OVMF/OVMF_VARS_4M.ms.fd   # clés Secure Boot Microsoft

USE_USB=0; USE_NET=0
for arg in "$@"; do
  case "$arg" in
    --usb) USE_USB=1 ;;
    --net) USE_NET=1 ;;
    *) echo "Option inconnue : $arg"; exit 1 ;;
  esac
done

[ -f "$ISO" ] || { echo "ISO absente : $ISO"; exit 1; }
[ -f "$DISK" ] || qemu-img create -f qcow2 "$DISK" 64G
[ -f "$VARS" ] || cp "$OVMF_VARS_TEMPLATE" "$VARS"
mkdir -p "$TPM_DIR"

# Identité matérielle propre à cette VM : par défaut, toutes les VM QEMU partagent les mêmes
# identifiants, et Windows peut alors confondre la VM avec une machine enregistrée ailleurs.
if [ ! -f "$ID_FILE" ]; then
  printf 'UUID=%s\nSERIAL=LAB-%s\n' "$(cat /proc/sys/kernel/random/uuid)" "$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')" > "$ID_FILE"
fi
# shellcheck source=/dev/null
. "$ID_FILE"

# TPM 2.0 émulé : s'arrête tout seul quand QEMU se déconnecte.
swtpm socket --tpm2 --tpmstate dir="$TPM_DIR" \
  --ctrl type=unixio,path="$TPM_SOCK" --terminate --daemon

USB_ARGS=()
if [ "$USE_USB" = 1 ]; then
  if ! lsusb -d 07ca:c835 >/dev/null; then
    echo "LGP Lite (07ca:c835) introuvable : branche-le d'abord."; exit 1
  fi
  # QEMU enregistre lui-même tout le trafic USB du boîtier (format usbmon) : pas besoin de sudo.
  mkdir -p "$DIR/../logs"
  PCAP="$DIR/../logs/win-usb-$(date +%Y%m%d-%H%M%S).pcap"
  USB_ARGS=(-device usb-host,bus=xhci.0,vendorid=0x07ca,productid=0xc835,pcap="$PCAP")
  echo "Le LGP Lite sera passé à la VM. Trafic USB enregistré dans $PCAP"
fi

NET_ARGS=(-nic none)
if [ "$USE_NET" = 1 ]; then
  NET_ARGS=(-netdev user,id=net0 -device e1000,netdev=net0)
  echo "Réseau activé."
else
  echo "VM sans réseau."
fi

exec qemu-system-x86_64 \
  -name win11-lab \
  -machine q35,smm=on,accel=kvm -cpu host -smp 3 -m 6G \
  -uuid "$UUID" -smbios type=1,manufacturer=Lab,product=GL310-Lab,serial="$SERIAL" \
  -global driver=cfi.pflash01,property=secure,value=on \
  -drive if=pflash,format=raw,unit=0,file="$OVMF_CODE",readonly=on \
  -drive if=pflash,format=raw,unit=1,file="$VARS" \
  -chardev socket,id=chrtpm,path="$TPM_SOCK" \
  -tpmdev emulator,id=tpm0,chardev=chrtpm -device tpm-tis,tpmdev=tpm0 \
  -device ahci,id=ahci \
  -drive id=disk,file="$DISK",if=none,format=qcow2 -device ide-hd,drive=disk,bus=ahci.0,bootindex=0 \
  -drive id=cd,file="$ISO",if=none,media=cdrom,readonly=on -device ide-cd,drive=cd,bus=ahci.1,bootindex=1 \
  -drive id=cd2,file="$DIR/gl310-driver.iso",if=none,media=cdrom,readonly=on -device ide-cd,drive=cd2,bus=ahci.2 \
  "${NET_ARGS[@]}" \
  -device qemu-xhci,id=xhci -device usb-tablet,bus=xhci.0 \
  "${USB_ARGS[@]}" \
  -monitor unix:"$MON_SOCK",server,nowait \
  -vga std -display gtk
