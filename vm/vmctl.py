#!/usr/bin/env python3
"""Pilote la console QEMU de la VM de laboratoire (socket ouvert par vm/start-vm.sh).

Usage :
  vmctl.py cmd "info status"         envoie une commande brute à la console
  vmctl.py keys spc 15 0.5           appuie 15 fois sur Espace, toutes les 0,5 s
  vmctl.py shot vm/screen.png        capture l'écran de la VM en PNG
  vmctl.py type 'e:\\install.cmd'     tape du texte (clavier AZERTY dans la VM)
  vmctl.py combo meta_l-r            envoie une combinaison de touches
"""
import os
import socket
import struct
import sys
import time
import zlib
from pathlib import Path

SOCK = f"/tmp/win11-lab-monitor-{os.getuid()}.sock"

# Clavier AZERTY (français) dans la VM : caractère -> touche QEMU (positions QWERTY).
AZERTY = {
    "a": "q", "q": "a", "z": "w", "w": "z", "m": "semicolon",
    " ": "spc", "\n": "ret", "\t": "tab",
    ":": "dot", ".": "shift-comma", ",": "m", ";": "comma", "!": "slash", "/": "shift-dot",
    "\\": "ctrl-alt-8", "-": "6", "_": "8", '"': "3", "'": "4", "(": "5", ")": "minus",
    "=": "equal", "%": "shift-apostrophe", "&": "1",
}
for d in "1234567890":
    AZERTY[d] = f"shift-{d}"


def azerty_key(ch):
    if ch in AZERTY:
        return AZERTY[ch]
    low = ch.lower()
    if low.isalpha() and low.isascii():
        base = AZERTY.get(low, low)
        return f"shift-{base}" if ch.isupper() else base
    raise ValueError(f"caractère non géré : {ch!r}")


def monitor(command, wait=0.3):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.settimeout(2)
    time.sleep(0.1)
    try:
        s.recv(65536)  # bannière
    except socket.timeout:
        pass
    s.sendall(command.encode() + b"\n")
    time.sleep(wait)
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out.decode(errors="replace")


def ppm_to_png(ppm_path, png_path):
    data = Path(ppm_path).read_bytes()
    parts = data.split(b"\n", 3)
    w, h = map(int, parts[1].split())
    pixels = parts[3]
    raw = b"".join(b"\x00" + pixels[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    Path(png_path).write_bytes(png)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return
    if argv[1] == "cmd":
        print(monitor(" ".join(argv[2:])))
    elif argv[1] == "keys":
        key, count, delay = argv[2], int(argv[3]), float(argv[4])
        for _ in range(count):
            monitor(f"sendkey {key}", wait=0.05)
            time.sleep(delay)
    elif argv[1] == "type":
        # Une seule connexion pour toute la frappe (bien plus rapide qu'une connexion par touche).
        text = " ".join(argv[2:]).replace("\\n", "\n")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCK)
        s.settimeout(0.2)
        for ch in text:
            s.sendall(f"sendkey {azerty_key(ch)}\n".encode())
            time.sleep(0.06)
            try:
                s.recv(65536)
            except socket.timeout:
                pass
        s.close()
    elif argv[1] == "combo":
        monitor(f"sendkey {argv[2]}", wait=0.1)
    elif argv[1] == "shot":
        png = Path(argv[2]).resolve()
        ppm = png.with_suffix(".ppm")
        monitor(f"screendump {ppm}", wait=1.0)
        ppm_to_png(ppm, png)
        ppm.unlink()
        print(png)


if __name__ == "__main__":
    main(sys.argv)
