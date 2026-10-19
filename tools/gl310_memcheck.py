#!/usr/bin/env python3
"""Relit (lecture seule) la mémoire où gl310_boot.py a chargé les firmwares, et la compare aux images.

Si l'ARM tourne, le firmware modifie ses variables et sa pile : des mots changent par rapport à
l'image envoyée, et certains changent entre deux lectures (compteurs).

Usage : gl310_memcheck.py [--passes 2] [--delay 1.0]
"""
import argparse
import struct
import time

import usb.core
import usb.util

from gl310_boot import (AUDIO_BASE, AUDIO_BSS_BLOCKS, BLOCK_WORDS, DATA_TIMEOUT_MS, EP_CMD_IN, EP_CMD_OUT,
                        EP_DATA_IN, CMD_TIMEOUT_MS, FW_AUDIO, FW_VIDEO, VIDEO_BASE, mem_header)


def read_region(dev, base, nbytes):
    out = bytearray()
    for off in range(0, nbytes, BLOCK_WORDS * 4):
        size = min(BLOCK_WORDS * 4, nbytes - off)
        dev.write(EP_CMD_OUT, mem_header(False, base + off // 4, size // 4).payload, CMD_TIMEOUT_MS)
        dev.read(EP_CMD_IN, 512, CMD_TIMEOUT_MS)
        block = bytearray()
        while len(block) < size:
            block += dev.read(EP_DATA_IN, size - len(block), DATA_TIMEOUT_MS)
        out += block
    return bytes(out)


def changed_words(a, b, base):
    words = []
    for i in range(0, min(len(a), len(b)), 4):
        if a[i:i + 4] != b[i:i + 4]:
            words.append((base + i // 4, struct.unpack("<I", a[i:i + 4])[0], struct.unpack("<I", b[i:i + 4])[0]))
    return words


def show(title, words, limit=12):
    print(f"{title} : {len(words)} mot(s) différent(s)")
    for addr, old, new in words[:limit]:
        print(f"    mot {addr:#07x} (octet {addr * 4:#08x}) : {old:#010x} -> {new:#010x}")
    if len(words) > limit:
        print(f"    … et {len(words) - limit} autres")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    dev = usb.core.find(idVendor=0x07CA, idProduct=0xC835)
    if dev is None:
        raise SystemExit("LGP Lite introuvable.")
    usb.util.claim_interface(dev, 0)

    audio, video = FW_AUDIO.read_bytes(), FW_VIDEO.read_bytes()
    bss_bytes = sum(AUDIO_BSS_BLOCKS) * 4
    regions = [
        ("vidéo", VIDEO_BASE, video),
        ("audio", AUDIO_BASE, audio),
        ("zéros après l'audio", AUDIO_BASE + len(audio) // 4, bytes(bss_bytes)),
    ]
    previous = {}
    for p in range(1, args.passes + 1):
        print(f"=== Lecture {p} ===")
        for name, base, image in regions:
            data = read_region(dev, base, len(image))
            show(f"{name} : différences avec l'image envoyée", changed_words(image, data, base))
            if name in previous:
                show(f"{name} : différences avec la lecture précédente", changed_words(previous[name], data, base))
            previous[name] = data
        if p < args.passes:
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
