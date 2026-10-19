#!/usr/bin/env python3
"""Outil de test du LGP Lite (GL310, 07ca:c835) en espace utilisateur, via libusb.

Protocole déduit de la trace Windows du projet octv_gears_lgp (LGP C875) :
  EP 0x04 OUT  commandes        EP 0x83 IN  réponses
  EP 0x02 OUT  firmwares/vidéo  EP 0x81 IN  données vidéo

Commandes connues sur EP 0x04 :
  01 00 01 00 <addr32>            lecture d'un registre
  01 01 01 00 <addr32> <val32>    écriture d'un registre
  0C 01 01 00 15 00 00 00 <reg>   lecture d'un registre du microcontrôleur (0x15)
  0B 01 02 00 15 00 00 00 <reg> <val>  écriture d'un registre du microcontrôleur
  (registre 0x2C du microcontrôleur = LED)

Usage :
  gl310.py info             descripteurs USB (lecture seule)
  gl310.py probe            lit quelques registres (lecture seule)
  gl310.py regread 0x38     lit un registre
  gl310.py mcuread 0x2c     lit un registre du microcontrôleur
  gl310.py led 0x05         écrit la valeur de la LED
  gl310.py blink            fait clignoter la LED (0x0b, 0x03, 0x05)
"""
import struct
import sys
import time

import usb.core
import usb.util

VENDOR = 0x07CA
PRODUCTS = (0xC835, 0xD835)  # d835 : identifiant probable après chargement du firmware
EP_CMD_OUT, EP_CMD_IN = 0x04, 0x83
MCU = 0x15
MCU_REG_LED = 0x2C
TIMEOUT_MS = 1000


def hexs(data):
    return " ".join(f"{b:02x}" for b in data)


def find_device():
    for pid in PRODUCTS:
        dev = usb.core.find(idVendor=VENDOR, idProduct=pid)
        if dev is not None:
            return dev
    sys.exit("LGP Lite introuvable (07ca:c835 ou 07ca:d835). Est-il branché ?")


def open_device():
    dev = find_device()
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
        if dev.get_active_configuration() is None:
            dev.set_configuration(1)
        usb.util.claim_interface(dev, 0)
    except usb.core.USBError as e:
        if e.errno == 13:
            sys.exit("Accès refusé : la règle udev n'est pas encore active (relance tools/setup-phase1.sh).")
        raise
    print(f"Ouvert : {dev.idVendor:04x}:{dev.idProduct:04x} (bus {dev.bus}, adresse {dev.address})")
    return dev


def command(dev, payload, expect_answer=True):
    payload = bytes(payload)
    print(f"  -> {hexs(payload)}")
    dev.write(EP_CMD_OUT, payload, TIMEOUT_MS)
    if not expect_answer:
        return None
    try:
        answer = bytes(dev.read(EP_CMD_IN, 512, TIMEOUT_MS))
        print(f"  <- {hexs(answer)}")
        return answer
    except usb.core.USBTimeoutError:
        print("  <- (pas de réponse, délai dépassé)")
        return None


def reg_read(dev, addr):
    return command(dev, b"\x01\x00\x01\x00" + struct.pack("<I", addr))


def mcu_read(dev, reg):
    return command(dev, bytes([0x0C, 0x01, 0x01, 0x00, MCU, 0, 0, 0, reg]))


def mcu_write(dev, reg, value):
    return command(dev, bytes([0x0B, 0x01, 0x02, 0x00, MCU, 0, 0, 0, reg, value]))


def cmd_info():
    dev = find_device()
    print(dev)


def cmd_probe():
    dev = open_device()
    # 0x38 et 0x50 : premiers registres lus par le pilote Windows.
    for addr in (0x38, 0x50, 0x00):
        print(f"Registre {addr:#06x}")
        reg_read(dev, addr)
    print(f"Microcontrôleur, registre LED {MCU_REG_LED:#04x}")
    mcu_read(dev, MCU_REG_LED)


def cmd_blink():
    dev = open_device()
    for value in (0x0B, 0x03, 0x05):
        print(f"LED <- {value:#04x}")
        mcu_write(dev, MCU_REG_LED, value)
        time.sleep(1)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    action, args = argv[1], [int(a, 0) for a in argv[2:]]
    if action == "info":
        cmd_info()
    elif action == "probe":
        cmd_probe()
    elif action == "regread":
        reg_read(open_device(), args[0])
    elif action == "mcuread":
        mcu_read(open_device(), args[0])
    elif action == "led":
        mcu_write(open_device(), MCU_REG_LED, args[0])
    elif action == "blink":
        cmd_blink()
    else:
        sys.exit(f"Action inconnue : {action}")


if __name__ == "__main__":
    main(sys.argv)
