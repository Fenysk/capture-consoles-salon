#!/usr/bin/env python3
"""Chargement « prudent » des firmwares du GL310 en RAM, puis démarrage de son processeur ARM.

Rejoue les lignes 225 à 370 de vendor/octv_gears_lgp/capture_sequence (trace Windows du C875),
avec les firmwares du pilote GL310 et sans aucune écriture I2C (elles sont toutes avant la ligne 225).
Chaque bloc de firmware est relu et comparé avant de démarrer l'ARM. Tout est en RAM :
débrancher le boîtier le remet dans son état d'origine.

Commandes utilisées (EP 0x04, adresses mémoire en mots de 4 octets) :
  07 0R 00 00 00 00 00 00                    ARM : R=0 arrêt, R=1 démarrage
  01 01 01 00 <reg32> <val32>                écriture d'un registre du pont USB
  01 00 01 00 <reg32>                        lecture d'un registre du pont USB
  02 01 04 00 <mot32> <mot32> <val32>        écriture d'un mot en mémoire ARM
  02 00 04 00 <mot32> <mot32>                lecture d'un mot en mémoire ARM
  09 0W 08 00 00 00 00 00 <mot32> <nmots32>  bloc mémoire : W=1 écriture (données sur EP 0x02),
                                             W=0 relecture (données sur EP 0x81)

Depuis le 2026-10-19, on commence par l'initialisation officielle (lignes 1 à 224 : 206 écritures I2C),
identique à ce qu'envoie le pilote Windows du GL310 (vérifié dans une VM). --no-i2c revient à l'ancien mode.

Usage :
  gl310_boot.py --dry-run   construit les commandes et les compare à la trace, sans toucher au boîtier
  gl310_boot.py [--no-i2c]  initialise, charge les firmwares et démarre l'ARM
"""
import argparse
import struct
import sys
import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW_AUDIO = ROOT / "vendor/gl310-driver/extracted/qpaudfwusb.bin"
FW_VIDEO = ROOT / "vendor/gl310-driver/extracted/qpvidfwusb.bin"
TRACE = ROOT / "vendor/octv_gears_lgp/capture_sequence"
TRACE_FIRST, TRACE_LAST = 225, 370
LOG_DIR = ROOT / "logs"

AUDIO_BASE = 0x40000                      # mots (octet 0x100000)
VIDEO_BASE = 0x00000
BLOCK_WORDS = 0x2000                      # 32 Ko
AUDIO_BSS_BLOCKS = (0x2000, 0x2000, 0x1E3B)  # zéros écrits juste après l'image audio

# Configuration de démarrage QPSOS v2 (mêmes adresses que QPSOS_CFG_BASE_V2 du pilote C985, en mots).
CFG_FIXED_MODE = 0x2F1094 // 4            # = 1
CFG_INT_MODE = 0x2F1090 // 4              # = 0 (polling)
CFG_BASE = 0x2F2000 // 4

EP_CMD_OUT, EP_CMD_IN, EP_DATA_OUT, EP_DATA_IN = 0x04, 0x83, 0x02, 0x81
CMD_TIMEOUT_MS, DATA_TIMEOUT_MS = 1000, 5000

# kind: "cmd" (EP 0x04), "data" (EP 0x02), "readback" (EP 0x81, compare avec `data`)
Step = namedtuple("Step", "kind expect payload label")


def cmd(payload, expect, label):
    return Step("cmd", expect, bytes(payload), label)


def reg_write(reg, val):
    return cmd(b"\x01\x01\x01\x00" + struct.pack("<II", reg, val), False, f"reg {reg:#06x} <- {val:#010x}")


def reg_read(reg):
    return cmd(b"\x01\x00\x01\x00" + struct.pack("<I", reg), True, f"reg {reg:#06x} ?")


def arm(run):
    return cmd(bytes([0x07, run, 0, 0, 0, 0, 0, 0]), False, "ARM démarrage" if run else "ARM arrêt")


def word_write(addr, val):
    return cmd(b"\x02\x01\x04\x00" + struct.pack("<III", addr, addr, val), False, f"mot {addr:#x} <- {val:#x}")


def word_read(addr):
    return cmd(b"\x02\x00\x04\x00" + struct.pack("<II", addr, addr), True, f"mot {addr:#x} ?")


def mem_header(write, addr, words):
    payload = bytes([0x09, 1 if write else 0, 0x08, 0x00, 0, 0, 0, 0]) + struct.pack("<II", addr, words)
    return cmd(payload, True, f"bloc {'écriture' if write else 'relecture'} {addr:#07x} x{words:#x} mots")


def mem_blocks(base, image, label, verify):
    steps = []
    for off in range(0, len(image), BLOCK_WORDS * 4):
        chunk = image[off:off + BLOCK_WORDS * 4]
        addr, words = base + off // 4, len(chunk) // 4
        steps += [mem_header(True, addr, words), Step("data", False, chunk, f"{label} +{off:#07x}")]
        if verify:
            steps += [mem_header(False, addr, words), Step("readback", True, chunk, f"vérif {label} +{off:#07x}")]
    return steps


def build_steps(audio, video):
    assert len(audio) % 4 == 0 and len(video) % 4 == 0
    steps = [
        arm(0),
        reg_write(0x06CC, 0),
        # Fenêtres mémoire du pont USB (identiques au tout début de la trace).
        reg_write(0x081C, 0x00004000), reg_write(0x0820, 0x00103FFF), reg_write(0x0824, 0x00000000),
        reg_write(0x0828, 0x00104000), reg_write(0x082C, 0x00203FFF), reg_write(0x0830, 0x00100000),
        reg_write(0x0834, 0x00204000), reg_write(0x0838, 0x00303FFF), reg_write(0x083C, 0x00200000),
        reg_write(0x0840, 0x70003124), reg_write(0x0840, 0x90003124),
        reg_read(0x0050), reg_write(0x0050, 0x0404), reg_read(0x0050), reg_write(0x0050, 0x0404),
        reg_write(0x0610, 0), reg_write(0x0614, 0),
        reg_read(0x0000), reg_write(0x0000, 0x07FF0000),
    ]
    bss = AUDIO_BASE + len(audio) // 4
    for words in AUDIO_BSS_BLOCKS:
        steps += [mem_header(True, bss, words), Step("data", False, bytes(words * 4), f"zéros {bss:#07x}")]
        bss += words
    steps += mem_blocks(AUDIO_BASE, audio, "audio", verify=True)
    steps += [word_write(CFG_FIXED_MODE, 1), word_write(CFG_INT_MODE, 0), word_write(CFG_BASE + 1, 0),
              reg_write(0x06CC, 0)]
    steps += mem_blocks(VIDEO_BASE, video, "vidéo", verify=True)
    steps += [
        arm(1),
        reg_write(0x06CC, 0),
        word_read(0x40), word_read(0x41), word_read(CFG_BASE + 4),  # signature QSOS, version, état
        reg_read(0x0610), reg_write(0x0610, 0), reg_read(0x0610), reg_write(0x0610, 0),
        reg_read(0x0610), reg_read(0x0618),
    ]
    return steps


OFFICIAL_INIT_LAST = 224


def official_init_steps():
    """Lignes 1 à 224 de la trace : réglages du pont USB et 206 écritures I2C (commande 05).

    Vérifié le 2026-10-19 : le pilote Windows officiel du GL310, capturé dans une VM
    (logs/win-usb-20261019-072048.pcap), envoie exactement ces 206 écritures I2C, octet pour octet.
    """
    steps = []
    for n, expect, ep, payload in parse_trace(1, OFFICIAL_INIT_LAST):
        if ep != 4:
            raise ValueError(f"ligne {n} : endpoint inattendu {ep}")
        label = f"init officielle l.{n}"
        if payload[0] == 0x05:
            label = f"I2C dev {payload[4]:#04x} reg {payload[8]:#04x} <- {payload[9:].hex(' ')}"
        steps.append(cmd(payload, expect, label))
    return steps


def parse_trace(first=TRACE_FIRST, last=TRACE_LAST):
    rows = []
    for n, line in enumerate(TRACE.read_text().splitlines(), 1):
        if first <= n <= last:
            t = line.split()
            rows.append((n, t[0] == "1", int(t[1]), bytes(int(x, 16) for x in t[2:])))
    return rows


def dry_run(steps):
    rows = parse_trace()
    kind_of_ep = {4: "cmd", 2: "data", 81: "readback"}
    print(f"{len(steps)} étapes construites, {len(rows)} lignes de trace ({TRACE_FIRST}-{TRACE_LAST})")
    if len(steps) != len(rows):
        print("ÉCART : nombre d'étapes différent de la trace")
        return False
    ok, video_started = True, False
    for step, (n, expect, ep, payload) in zip(steps, rows):
        video_started |= step.label.startswith("vidéo")
        if kind_of_ep[ep] != step.kind or expect != step.expect:
            print(f"ÉCART ligne {n} : type/réponse attendue différents ({step.label})")
            ok = False
        elif step.kind == "cmd" and step.payload != payload:
            # Seule différence admise : la longueur du dernier bloc vidéo (firmware GL310 un peu plus long).
            tolerated = video_started and step.payload[:12] == payload[:12]
            print(f"{'diff tolérée' if tolerated else 'ÉCART'} ligne {n} : {step.payload.hex(' ')} | trace {payload.hex(' ')}")
            ok &= tolerated
        elif step.kind == "data" and not video_started and step.payload != payload:
            print(f"ÉCART ligne {n} : données différentes de la trace ({step.label})")
            ok = False
    print("Plan conforme à la trace." if ok else "Plan NON conforme : rien ne sera envoyé.")
    return ok


class Box:
    def __init__(self, log):
        import usb.core
        import usb.util
        self.usb = usb
        self.log = log
        self.dev = None
        for pid in (0xC835, 0xD835):
            self.dev = usb.core.find(idVendor=0x07CA, idProduct=pid)
            if self.dev is not None:
                break
        if self.dev is None:
            sys.exit("LGP Lite introuvable.")
        if self.dev.idProduct != 0xC835:
            sys.exit(f"Le boîtier est en {self.dev.idProduct:04x}, pas en c835 : débranche-le 10 s puis rebranche-le.")
        usb.util.claim_interface(self.dev, 0)
        self.say(f"Ouvert : 07ca:{self.dev.idProduct:04x} (bus {self.dev.bus}, adresse {self.dev.address})")

    def say(self, text):
        print(text)
        self.log.write(text + "\n")

    def read_exact(self, ep, size, timeout):
        buf = bytearray()
        while len(buf) < size:
            buf += self.dev.read(ep, size - len(buf), timeout)
        return bytes(buf)

    def run(self, step):
        if step.kind == "cmd":
            self.dev.write(EP_CMD_OUT, step.payload, CMD_TIMEOUT_MS)
            answer = bytes(self.dev.read(EP_CMD_IN, 512, CMD_TIMEOUT_MS)) if step.expect else None
            self.say(f"{step.label:42} -> {step.payload.hex(' ')}" + (f"  <- {answer.hex(' ')}" if answer is not None else ""))
            return answer
        if step.kind == "data":
            self.dev.write(EP_DATA_OUT, step.payload, DATA_TIMEOUT_MS)
            self.say(f"{step.label:42} -> {len(step.payload)} octets")
            return None
        got = self.read_exact(EP_DATA_IN, len(step.payload), DATA_TIMEOUT_MS)
        if got != step.payload:
            first = next(i for i in range(len(got)) if got[i] != step.payload[i])
            raise RuntimeError(f"{step.label} : relecture différente à partir de l'octet {first:#x}")
        self.say(f"{step.label:42} OK ({len(got)} octets identiques)")
        return None


def list_box():
    import usb.core
    found = [f"07ca:{d.idProduct:04x} (bus {d.bus}, adresse {d.address})" for d in usb.core.find(find_all=True, idVendor=0x07CA)]
    return ", ".join(found) or "aucun périphérique AVerMedia"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="vérifie le plan sans toucher au boîtier")
    parser.add_argument("--no-i2c", action="store_true",
                        help="saute l'initialisation officielle (206 écritures I2C), comme la phase 2 prudente")
    args = parser.parse_args()

    steps = build_steps(FW_AUDIO.read_bytes(), FW_VIDEO.read_bytes())
    if not dry_run(steps):
        sys.exit(1)
    pre = [] if args.no_i2c else official_init_steps()
    print(f"Initialisation officielle : {len(pre)} commandes" + (" (désactivée)" if args.no_i2c else ""))
    if args.dry_run:
        return
    steps = pre + steps

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"boot-{datetime.now():%Y%m%d-%H%M%S}.log"
    with open(log_path, "w") as log:
        box = Box(log)
        try:
            for step in steps:
                box.run(step)
        except Exception as e:  # noqa: BLE001 — on veut s'arrêter net et garder la trace de l'erreur
            box.say(f"ARRÊT : {e}")
            box.say("Rien n'est permanent : débranche le boîtier 10 s puis rebranche-le pour repartir de zéro.")
            sys.exit(2)
        box.say("Séquence terminée. Attente de 3 s pour voir si le boîtier change d'identifiant...")
        time.sleep(3)
        box.say(f"USB maintenant : {list_box()}")
    print(f"Journal : {log_path}")


if __name__ == "__main__":
    main()
