#!/usr/bin/env python3
"""Récupère les images encodées par le GL310 (après gl310_boot.py puis gl310_encode.py).

Boucle (protocole du pilote C985, même firmware QPSOS) :
  1. attendre 0x6C8 bit 0 (le firmware a posté un message) ;
  2. lire 0x6B0..0x6C4 : message (task<<16 | 0x40 = image prête), étiquette (0x83 = flux vidéo),
     adresse en mots, adresse 2, taille en mots, PTS ;
  3. lire la mémoire du boîtier à cette adresse (commande 09 00, données sur EP 0x81) ;
  4. libérer 0x6C8 (bit 0 à 0) puis rendre le tampon au firmware (message 0x30 CompleteArm).

Le flux est du MPEG-TS dont les octets arrivent inversés par mots de 4 octets
(« 30 00 40 47 » = paquet TS « 47 40 00 30 ») : on les remet dans l'ordre.
Chaque message contient un nombre entier de paquets TS de 188 octets (47 mots).

Usage : gl310_capture.py [--frames 60] [--out captures/capture.ts]
"""
import argparse
import struct
import time
from pathlib import Path

import usb.core
import usb.util

ROOT = Path(__file__).resolve().parent.parent
EP_CMD_OUT, EP_CMD_IN, EP_DATA_IN = 0x04, 0x83, 0x81
BLOCK_WORDS = 0x2000


class Box:
    def __init__(self):
        self.dev = usb.core.find(idVendor=0x07CA, idProduct=0xC835)
        if self.dev is None:
            raise SystemExit("LGP Lite introuvable.")
        usb.util.claim_interface(self.dev, 0)
        self.flush()

    def flush(self):
        """Vide les données restées en attente (capture interrompue en plein transfert)."""
        for ep in (EP_DATA_IN, EP_CMD_IN):
            try:
                while True:
                    self.dev.read(ep, 65536, 50)
            except usb.core.USBError:
                pass

    def raw(self, payload, expect=True, timeout=1000):
        self.dev.write(EP_CMD_OUT, payload, timeout)
        return bytes(self.dev.read(EP_CMD_IN, 512, timeout)) if expect else None

    def reg_read(self, reg, count=1):
        a = self.raw(b"\x01\x00" + bytes([count]) + b"\x00" + struct.pack("<I", reg))
        return list(struct.unpack(f"<{count}I", a[:4 * count]))

    def reg_write(self, reg, val):
        self.raw(b"\x01\x01\x01\x00" + struct.pack("<II", reg, val), expect=False)

    def mem_read(self, addr_words, nbytes):
        out = bytearray()
        words_total = (nbytes + 3) // 4
        done = 0
        while done < words_total:
            n = min(BLOCK_WORDS, words_total - done)
            self.raw(bytes([0x09, 0x00, 0x08, 0x00, 0, 0, 0, 0]) + struct.pack("<II", addr_words + done, n))
            block = bytearray()
            while len(block) < n * 4:
                block += self.dev.read(EP_DATA_IN, n * 4 - len(block), 5000)
            out += block
            done += n
        return bytes(out[:nbytes])

    def mbox_send(self, opcode, task, param0, slots):
        for _ in range(200):
            if not self.reg_read(0x06CC)[0] & 1:
                break
            time.sleep(0.001)
        self.reg_write(0x06F8, param0)
        for i, v in enumerate(slots):
            self.reg_write(0x06F4 - 4 * i, v)
        self.reg_write(0x06CC, (task << 16) | 1)
        self.reg_write(0x06FC, (task << 16) | opcode)


def release(box, st, msg, p0, size, pts):
    """Libère 0x6C8, envoie l'accusé si demandé, et rend le tampon d'une image (0x30)."""
    kind, task = msg & 0xFFFF, msg >> 16
    box.reg_write(0x06C8, st & ~1)
    if st & 0x100:
        box.mbox_send(0x31 if kind < 0x80 else 0xA2, task, 0, [])
    if (kind & 0xFF) == 0x40:
        box.mbox_send(0x30, task, p0 & 0xFFFF, [size, pts & 0x7FFFFFFF, pts >> 31, 0, (p0 >> 24) & 0xFF])


def drain(box, limit=2000):
    """Rend sans les lire les messages accumulés : on s'arrête dès que plus rien n'attend,
    c'est-à-dire quand le firmware livre au rythme réel (une image toutes les ~33 ms)."""
    count = 0
    while count < limit:
        st = box.reg_read(0x06C8)[0]
        if not st & 1:
            break
        msg, p0, _addr, _addr2, size, pts = box.reg_read(0x06B0, 6)
        release(box, st, msg, p0, size, pts)
        count += 1
    return count


def word_swap(data):
    """Remet dans l'ordre les octets de chaque mot de 4 octets."""
    words = len(data) // 4
    return struct.pack(f">{words}I", *struct.unpack(f"<{words}I", data[:words * 4]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--out", default=str(ROOT / "captures/capture.ts"))
    args = parser.parse_args()
    out_path = Path(args.out)
    out_path.parent.mkdir(exist_ok=True)

    box = Box()
    drained = drain(box)
    print(f"{drained} message(s) en retard rendus sans lecture (leurs données étaient peut-être déjà écrasées).")
    frames, total, tags = 0, 0, {}
    t0 = time.time()
    with open(out_path, "wb") as out:
        while frames < args.frames and time.time() - t0 < args.timeout:
            st = box.reg_read(0x06C8)[0]
            if not st & 1:
                time.sleep(0.002)
                continue
            msg, p0, addr, addr2, size, pts = box.reg_read(0x06B0, 6)
            if (msg & 0xFF) != 0x40:
                print(f"message {msg:#010x} (non-image) : {p0:#x} {addr:#x} {addr2:#x} {size:#x} {pts:#x}")
                release(box, st, msg, p0, size, pts)
                continue
            # Lire les données AVANT de rendre le tampon, sinon le firmware peut le réécrire.
            tag = p0 & 0xFFFF
            data = word_swap(box.mem_read(addr, size * 4))
            release(box, st, msg, p0, size, pts)
            tags[tag] = tags.get(tag, 0) + 1
            if tag == 0x83:
                out.write(data)
            frames += 1
            total += len(data)
            if frames <= 5 or frames % 30 == 0:
                print(f"image {frames:4} : étiquette {tag:#x}, adresse {addr:#x}, {size} mots ({size * 4 // 188} paquets TS), début {data[:8].hex(' ')}")
    dt = time.time() - t0
    print(f"{frames} message(s) d'image en {dt:.1f} s ({frames / dt:.1f}/s), {total} octets, étiquettes {tags}")
    print(f"Flux écrit dans {out_path}")


if __name__ == "__main__":
    main()
