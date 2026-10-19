#!/usr/bin/env python3
"""Décode une capture usbmon (.pcapng, faite avec vm/capture-usb.sh) du LGP Lite en opérations lisibles.

Pour chaque échange avec le boîtier (07ca:c835), on affiche :
  - EP 0x04 (commandes) : REG R/W, MCU R/W (0C/0B), I2C 05, mot ARM (02), bloc mémoire (09), ARM (07)
  - EP 0x83 (réponses)  : la réponse brute, accolée à la commande
  - EP 0x02 / 0x81      : taille des blocs (et correspondance avec les firmwares connus)

Usage :
  usbpcap_decode.py logs/win-trace-XXXX.pcapng [--dev N] [--all] [--seq sortie.txt]
    --dev N   numéro de périphérique USB du boîtier (sinon : détecté via les descripteurs 07ca)
    --all     n'agrège pas les lectures répétées de registres (0x0610/0x0618, 0x0800, 0x06CC…)
    --seq F   écrit aussi la séquence au format capture_sequence (rejouable)
"""
import argparse
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRMWARES = {
    "audio GL310": ROOT / "vendor/gl310-driver/extracted/qpaudfwusb.bin",
    "vidéo GL310": ROOT / "vendor/gl310-driver/extracted/qpvidfwusb.bin",
}
NOISY_REGS = {0x0610, 0x0618, 0x0800, 0x06CC, 0x06C8}


def tshark_rows(path):
    fields = ["frame.number", "frame.time_relative", "usb.device_address", "usb.endpoint_address",
              "usb.urb_type", "usb.idVendor", "usb.capdata"]
    cmd = ["tshark", "-r", str(path), "-T", "fields", "-E", "separator=\t", "-E", "occurrence=f"]
    for f in fields:
        cmd += ["-e", f]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        parts = (line.split("\t") + [""] * len(fields))[:len(fields)]
        num, t, dev, ep, urb, vid, data = parts
        yield {
            "n": int(num), "t": float(t or 0), "dev": int(dev) if dev else None,
            "ep": int(ep, 16) if ep.startswith("0x") else (int(ep) if ep else None),
            "urb": urb.strip("'\""), "vid": vid, "data": bytes.fromhex(data.replace(":", "")) if data else b"",
        }


def describe(b):
    if len(b) < 4:
        return "RAW " + b.hex(" ")
    op, rw = b[0], b[1]
    if op == 0x01 and rw == 1 and len(b) >= 12:
        reg, val = struct.unpack("<II", b[4:12])
        return f"REG W {reg:#06x} = {val:#010x}"
    if op == 0x01 and rw == 0 and len(b) >= 8:
        return f"REG R {struct.unpack('<I', b[4:8])[0]:#06x}" + (f" x{b[2]}" if b[2] > 1 else "")
    if op == 0x0C and len(b) >= 9:
        return f"MCU R reg {b[8]:#04x} x{b[2]}"
    if op == 0x0B and len(b) >= 9:
        return f"MCU W reg {b[8]:#04x} = {b[9:].hex(' ')}"
    if op == 0x05 and len(b) >= 9:
        return f"I2C05 {'W' if rw else 'R?'} dev {b[4]:#04x} reg {b[8]:#04x} = {b[9:].hex(' ')}"
    if op == 0x02 and len(b) >= 12:
        addr = struct.unpack("<I", b[4:8])[0]
        return f"MOT {'W' if rw else 'R'} {addr:#x}" + (f" = {struct.unpack('<I', b[12:16])[0]:#x}" if rw and len(b) >= 16 else "")
    if op == 0x09 and len(b) >= 16:
        addr, words = struct.unpack("<II", b[8:16])
        return f"BLOC {'W' if rw else 'R'} mot {addr:#x} x{words:#x} mots"
    if op == 0x07:
        return f"ARM {'démarrage' if rw else 'arrêt'}"
    return "RAW " + b.hex(" ")


def find_device(rows, wanted):
    if wanted is not None:
        return wanted
    for r in rows:
        if r["vid"] and r["vid"].lower() in ("0x07ca", "0x7ca", "1994") and r["dev"] is not None:
            return r["dev"]
    # À défaut : le périphérique qui parle sur les endpoints 0x04 et 0x83.
    seen = {}
    for r in rows:
        if r["ep"] in (0x04, 0x83) and r["dev"] is not None:
            seen[r["dev"]] = seen.get(r["dev"], 0) + 1
    return max(seen, key=seen.get) if seen else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("pcap")
    p.add_argument("--dev", type=int)
    p.add_argument("--all", action="store_true")
    p.add_argument("--seq")
    args = p.parse_args()

    rows = list(tshark_rows(args.pcap))
    dev = find_device(rows, args.dev)
    if dev is None:
        sys.exit("Boîtier introuvable dans la capture.")
    fw = {name: path.read_bytes() for name, path in FIRMWARES.items() if path.exists()}
    print(f"Boîtier : périphérique {dev}, {len(rows)} trames au total")

    seq = []
    pending = None          # dernière commande EP 0x04 en attente de réponse
    last_key, repeat = None, 0

    def emit(text, key=None):
        nonlocal last_key, repeat
        if not args.all and key is not None and key == last_key:
            repeat += 1
            return
        if repeat:
            print(f"        (répété x{repeat + 1})")
        last_key, repeat = key, 0
        print(text)

    for r in rows:
        if r["dev"] != dev or r["ep"] is None:
            continue
        ep, data, urb = r["ep"], r["data"], r["urb"]
        out_ep = not (ep & 0x80)
        # OUT : données à la soumission (S) ; IN : données à la complétion (C).
        if out_ep and urb not in ("S", "83") or (not out_ep and urb not in ("C", "67")):
            continue
        if not data:
            continue
        if ep == 0x04:
            pending = (r, data)
            seq.append(f"1 4 {data.hex(' ').upper()}")
            desc = describe(data)
            noisy = data[0] == 0x01 and data[1] == 0 and len(data) >= 8 and struct.unpack("<I", data[4:8])[0] in NOISY_REGS
            emit(f"{r['t']:9.3f}s #{r['n']:<6} {desc}", key=desc if noisy else None)
        elif ep == 0x83:
            if pending and last_key is None:
                print(f"{'':19}   <- {data.hex(' ')}")
            pending = None
        elif ep == 0x02:
            seq.append(f"0 2 {data.hex(' ').upper()}")
            match = ""
            for name, blob in fw.items():
                i = blob.find(data[:4096])
                if i >= 0:
                    match = f" = {name} @ {i:#x}" + ("" if blob[i:i + len(data)] == data else " (partiel)")
                    break
            emit(f"{r['t']:9.3f}s #{r['n']:<6}       données EP02 : {len(data)} octets{match or (' (zéros)' if not any(data) else '')}")
        elif ep == 0x81:
            seq.append("1 81")
            emit(f"{r['t']:9.3f}s #{r['n']:<6}       données EP81 : {len(data)} octets, début {data[:8].hex(' ')}", key="EP81")
    if repeat:
        print(f"        (répété x{repeat + 1})")
    if args.seq:
        Path(args.seq).write_text("\n".join(seq) + "\n")
        print(f"Séquence écrite : {args.seq} ({len(seq)} lignes)")


if __name__ == "__main__":
    main()
