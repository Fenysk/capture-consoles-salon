#!/usr/bin/env python3
"""Démarrage de l'encodeur du GL310 (après gl310_boot.py), puis observation des réponses du firmware.

Rejoue les lignes 371 à 757 de la trace Windows du C875 en ne gardant que :
  - les accès aux registres du pont USB (boîte aux lettres 0x6B0-0x6FC, 0x0800, GPIO en lecture) ;
  - les LECTURES du microcontrôleur (0C), sans effet.
Sont retirées : toutes les écritures au microcontrôleur (0B) et toutes les écritures I2C (05).

Boîte aux lettres (même disposition que le pilote C985) :
  0x6F8..0x6D0 paramètres, 0x6CC état (bit 0 = message en attente), 0x6FC opcode,
  réponses du firmware : 0x6C8 état (bit 0 = réponse prête), 0x6B0..0x6C4 contenu.
Séquence : 0x0A, F1 SystemOpen, F2 SystemLink, réglages 0x10, bloc de config 1080p, 0x01 StartEncoder.

Par défaut (désactivable avec --no-audio-pass) : met à 1 le bit 16 (aou_pass_en) de AUDIO_CONTROL_PARAM (registre 0x6D4 du bloc
de config, 0x21161080 -> 0x21171080), d'après la façon dont le pilote Windows découpe ce champ.
Bits : 0-9 débit audio, 10-11 ch_sel, 12-15 sample_rate, 16 aou_pass_en, 17 ai_msb, 18 lrclk_i, 19 bclk_i,
20 ai_i2s, 21 ai_rj, 22 ai_m, 23-24 ch_num, 25 pts, 26 disAi, 27 raw, 28-31 audio_type.

Usage : gl310_encode.py [--observe 5] [--no-audio-pass] [--prudent]
"""
import argparse
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import usb.core
import usb.util

ROOT = Path(__file__).resolve().parent.parent
TRACE = ROOT / "vendor/octv_gears_lgp/capture_sequence"
FIRST, LAST = 371, 757
LOG_DIR = ROOT / "logs"
EP_CMD_OUT, EP_CMD_IN, EP_DATA_IN = 0x04, 0x83, 0x81
REG_TO_ARM_STATUS, REG_FROM_ARM_STATUS, REG_FROM_ARM_MSG = 0x06CC, 0x06C8, 0x06B0


class Box:
    def __init__(self, log):
        self.log = log
        self.dev = usb.core.find(idVendor=0x07CA, idProduct=0xC835)
        if self.dev is None:
            sys.exit("LGP Lite introuvable.")
        usb.util.claim_interface(self.dev, 0)

    def say(self, text):
        print(text)
        self.log.write(text + "\n")

    def raw(self, payload, expect):
        self.dev.write(EP_CMD_OUT, payload, 1000)
        return bytes(self.dev.read(EP_CMD_IN, 512, 1000)) if expect else None

    def reg_read(self, reg, count=1):
        answer = self.raw(b"\x01\x00" + bytes([count]) + b"\x00" + struct.pack("<I", reg), True)
        return list(struct.unpack(f"<{len(answer) // 4}I", answer[:len(answer) // 4 * 4]))

    def reg_write(self, reg, val):
        self.raw(b"\x01\x01\x01\x00" + struct.pack("<II", reg, val), False)

    def mcu_read(self, reg, n):
        return self.raw(bytes([0x0C, 0x01, n, 0x00, 0x15, 0, 0, 0, reg]), True)


def trace_steps():
    for n, line in enumerate(TRACE.read_text().splitlines(), 1):
        if FIRST <= n <= LAST:
            t = line.split()
            yield n, t[0] == "1", int(t[1]), bytes(int(x, 16) for x in t[2:])


def describe(b):
    if b[0] == 1 and b[1] == 1:
        reg, val = struct.unpack("<II", b[4:12])
        return f"reg {reg:#06x} <- {val:#010x}"
    if b[0] == 1 and b[1] == 0:
        return f"reg {struct.unpack('<I', b[4:8])[0]:#06x} ?"
    if b[0] == 0x0C:
        return f"micro reg {b[8]:#04x} ? x{b[2]}"
    return b.hex(" ")


REG_AUDIO_CONTROL_PARAM = 0x06D4
AOU_PASS_EN = 1 << 16


def bcd(v):
    return ((v // 10) << 4) | (v % 10)


def rtc_now():
    """Date et heure actuelles au format BCD du micro (AA MM JJ hh mm ss), comme le pilote Windows."""
    t = datetime.now()
    return bytes(bcd(x) for x in (t.year % 100, t.month, t.day, t.hour, t.minute, t.second))


def replay(box, audio_pass=False, official=True, patch=None):
    """Rejoue la trace. official=True : garde aussi les écritures micro (0B) et I2C (05), comme le fait
    le pilote Windows du GL310 (vérifié en VM pour 0x43/0x44/0x42/0x41/0x30/0x2F/0x40/0x2C).
    patch(ligne, commande) -> commande : permet de remplacer une commande de la trace (réglages de GL310 Studio)."""
    skipped = 0
    for n, expect, ep, b in trace_steps():
        if patch is not None and ep == 4:
            b = patch(n, b)
        if audio_pass and b[:4] == b"\x01\x01\x01\x00" and struct.unpack("<I", b[4:8])[0] == REG_AUDIO_CONTROL_PARAM:
            value = struct.unpack("<I", b[8:12])[0] | AOU_PASS_EN
            b = b[:8] + struct.pack("<I", value)
            box.say(f"{n:4}: AUDIO_CONTROL_PARAM avec aou_pass_en -> {value:#010x}")
        if ep != 4:
            continue
        if b[0] in (0x0B, 0x05):
            if not official:
                skipped += 1
                continue
            if b[0] == 0x0B and b[8] in (0x47, 0x49):
                raise RuntimeError(f"ligne {n} : écriture du profil permanent du micro ({b[8]:#04x}), refusée")
            if b[0] == 0x0B and b[8] == 0x30:  # horloge : date du jour plutôt que celle de 2014
                b = b[:9] + rtc_now()
            answer = box.raw(b, expect)
            box.say(f"{n:4}: {'MCU' if b[0] == 0x0B else 'I2C'} {b.hex(' ')}" + (f" -> {answer.hex(' ')}" if answer else ""))
            continue
        if b[0] not in (0x01, 0x0C):
            box.say(f"ligne {n} : commande inattendue {b.hex(' ')}, arrêt")
            return False
        if b[:2] == b"\x01\x00" and struct.unpack("<I", b[4:8])[0] == REG_TO_ARM_STATUS:
            # Avant chaque message : attendre que le firmware ait consommé le précédent (bit 0 à 0).
            for _ in range(50):
                status = box.reg_read(REG_TO_ARM_STATUS)[0]
                if not status & 1:
                    break
                time.sleep(0.01)
            box.say(f"{n:4}: reg 0x06cc ? -> {status:#010x}" + ("  (OCCUPÉ, message précédent non consommé)" if status & 1 else ""))
            continue
        answer = box.raw(b, expect)
        if b[0] == 0x01 and b[1] == 1:
            reg = struct.unpack("<I", b[4:8])[0]
            if reg == 0x06FC:
                box.say(f"{n:4}: {describe(b)}   <== message {struct.unpack('<I', b[8:12])[0]:#04x} envoyé")
                continue
        if answer is not None and not (b[0] == 1 and struct.unpack("<I", b[4:8])[0] in (0x0610, 0x0618)):
            box.say(f"{n:4}: {describe(b)} -> {answer.hex(' ')}")
    box.say(f"{skipped} écritures micro/I2C retirées." if not official else "Écritures micro/I2C officielles incluses.")
    return True


def observe(box, seconds):
    box.say(f"--- Observation pendant {seconds} s ---")
    last = None
    end = time.time() + seconds
    got_data = 0
    while time.time() < end:
        st = box.reg_read(REG_FROM_ARM_STATUS)[0]
        msg = box.reg_read(REG_FROM_ARM_MSG, 6)
        arm = box.reg_read(0x0800)[0]
        snap = (st, tuple(msg), arm)
        if snap != last:
            box.say(f"0x6C8={st:#010x}  0x6B0..0x6C4={' '.join(f'{v:08x}' for v in msg)}  0x800={arm:#010x}")
            last = snap
        try:
            data = bytes(box.dev.read(EP_DATA_IN, 16384, 50))
            got_data += len(data)
            box.say(f"EP 0x81 : {len(data)} octets spontanés, début {data[:16].hex(' ')}")
        except usb.core.USBTimeoutError:
            pass
        time.sleep(0.05)
    box.say(f"Données spontanées reçues sur EP 0x81 : {got_data} octets")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--observe", type=float, default=5)
    parser.add_argument("--no-audio-pass", action="store_true",
                        help="laisse aou_pass_en à 0 (par défaut il est activé : son sur la prise AUDIO OUT)")
    parser.add_argument("--prudent", action="store_true", help="retire les écritures micro/I2C (ancien mode)")
    args = parser.parse_args()
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"encode-{datetime.now():%Y%m%d-%H%M%S}.log"
    with open(log_path, "w") as log:
        box = Box(log)
        sig = box.raw(b"\x02\x00\x04\x00" + struct.pack("<II", 0x40, 0x40), True)
        if sig[:4] != b"QSOS":
            sys.exit("Firmware absent (débranché ?) : relance d'abord gl310_boot.py.")
        box.say(f"Firmware présent. Micro : état 0x1C = {box.mcu_read(0x1C, 1).hex(' ')}, timing = {box.mcu_read(0x1D, 7).hex(' ')}")
        if replay(box, audio_pass=not args.no_audio_pass, official=not args.prudent):
            observe(box, args.observe)
    print(f"Journal : {log_path}")


if __name__ == "__main__":
    main()
